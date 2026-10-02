"""메인 창: 화면 캡처 뷰 + 템플릿 목록 + 스텝 목록 + 실행/로그."""
import os
import re
import subprocess
import threading

import cv2
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QToolBar,
                               QVBoxLayout, QWidget)

from . import vision
from .adb import ROOT, NO_WINDOW, Adb, find_tool
from .model import STEP_GROUPS, STEP_TYPES, Macro, describe, new_step
from .runner import Runner
from .step_dialog import StepDialog

MACRO_DIR = os.path.join(ROOT, "macros")


def to_pixmap(img):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QPixmap.fromImage(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())


class ScreenView(QWidget):
    """캡처 이미지를 비율 유지해 보여주고, 클릭/드래그를 이미지 좌표로 알려준다."""
    point_clicked = Signal(int, int)
    region_selected = Signal(int, int, int, int)
    hovered = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 400)
        self.setMouseTracking(True)
        self.img = None
        self.pix = None
        self.boxes = []  # (x, y, w, h, QColor, text)
        self._drag_from = None
        self._drag_to = None

    def set_image(self, img):
        self.img = img
        self.pix = to_pixmap(img)
        self.update()

    def set_boxes(self, boxes):
        self.boxes = boxes
        self.update()

    def _target(self):
        if not self.pix:
            return QRect(), 1.0
        s = min(self.width() / self.pix.width(), self.height() / self.pix.height())
        w, h = int(self.pix.width() * s), int(self.pix.height() * s)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), s

    def _to_img(self, pos):
        r, s = self._target()
        if not self.pix:
            return None
        x = int((pos.x() - r.x()) / s)
        y = int((pos.y() - r.y()) / s)
        x = max(0, min(self.pix.width() - 1, x))
        y = max(0, min(self.pix.height() - 1, y))
        return QPoint(x, y)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        if not self.pix:
            p.setPen(QColor("#aaa"))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "기기를 연결하고 [화면 캡처](F5)를 누르세요\n\n"
                       "드래그 → 버튼 이미지를 템플릿으로 등록\n클릭 → 그 좌표로 탭/스와이프 스텝 추가")
            return
        r, s = self._target()
        p.drawPixmap(r, self.pix)
        p.setFont(QFont("Malgun Gothic", 9))
        for x, y, w, h, color, text in self.boxes:
            p.setPen(QPen(color, 2))
            p.drawRect(int(r.x() + x * s), int(r.y() + y * s), int(w * s), int(h * s))
            if text:
                p.drawText(int(r.x() + x * s), int(r.y() + y * s) - 4, text)
        if self._drag_from and self._drag_to:
            p.setPen(QPen(QColor("#00e5ff"), 2, Qt.DashLine))
            p.drawRect(QRect(self._drag_from, self._drag_to).normalized())

    def mousePressEvent(self, e):
        if self.pix and e.button() == Qt.LeftButton:
            self._drag_from = e.position().toPoint()
            self._drag_to = None

    def mouseMoveEvent(self, e):
        pt = self._to_img(e.position().toPoint())
        if pt:
            self.hovered.emit(pt.x(), pt.y())
        if self._drag_from:
            self._drag_to = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if not self._drag_from or e.button() != Qt.LeftButton:
            return
        a = self._to_img(self._drag_from)
        b = self._to_img(e.position().toPoint())
        moved = (e.position().toPoint() - self._drag_from).manhattanLength()
        self._drag_from = self._drag_to = None
        self.update()
        if moved < 6:
            self.point_clicked.emit(a.x(), a.y())
        else:
            x, y = min(a.x(), b.x()), min(a.y(), b.y())
            w, h = abs(a.x() - b.x()), abs(a.y() - b.y())
            if w >= 4 and h >= 4:
                self.region_selected.emit(x, y, w, h)


class _Bg(QObject):
    """백그라운드 작업 결과를 UI 스레드로 돌려주는 다리."""
    done = Signal(object, object, object)  # callback, result, error


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("모두의 매크로 - 게임 매크로 생성기")
        self.resize(1280, 820)
        self.adb = Adb()
        self.macro = None
        self.runner = None
        self.swipe_start = None
        self._bg = _Bg()
        self._bg.done.connect(lambda cb, res, err: cb(res, err))
        self._build_ui()
        self.refresh_devices()
        os.makedirs(MACRO_DIR, exist_ok=True)
        self.log("adb: " + (self.adb.path or "없음 (tools 폴더 확인)"))

    # ---------- UI 구성 ----------
    def _build_ui(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" 기기 "))
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(200)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        tb.addWidget(self.device_box)
        self._act(tb, "새로고침", self.refresh_devices)
        self._act(tb, "무선 연결", self.wireless_connect)
        self._act(tb, "scrcpy 미러링", self.open_scrcpy)
        self._act(tb, "화면 캡처 (F5)", self.capture, "F5")
        tb.addSeparator()
        self._act(tb, "새 매크로", self.new_macro, "Ctrl+N")
        self._act(tb, "열기", self.open_macro, "Ctrl+O")
        self._act(tb, "저장", self.save_macro, "Ctrl+S")
        self._act(tb, "실행 설정", self.edit_settings)

        self.screen = ScreenView()
        self.screen.point_clicked.connect(self._on_point)
        self.screen.region_selected.connect(self._on_region)
        self.screen.hovered.connect(lambda x, y: self.statusBar().showMessage(f"좌표 ({x}, {y})"))

        # 템플릿
        self.tmpl_list = QListWidget()
        self.tmpl_list.setViewMode(QListWidget.IconMode)
        self.tmpl_list.setIconSize(QSize(72, 72))
        self.tmpl_list.setResizeMode(QListWidget.Adjust)
        self.tmpl_list.setMaximumHeight(170)
        self.tmpl_list.itemDoubleClicked.connect(lambda _: self.test_template())
        tmpl_btns = self._buttons([("매칭 테스트", self.test_template), ("이름 변경", self.rename_template),
                                   ("삭제", self.delete_template)])

        # 스텝
        self.step_list = QListWidget()
        self.step_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.step_list.model().rowsMoved.connect(lambda *_: QTimer.singleShot(0, self._steps_reordered))
        self.step_list.itemDoubleClicked.connect(lambda _: self.edit_step())
        add_btn = QPushButton("스텝 추가 ▾")
        menu = QMenu(self)
        for group, types in STEP_GROUPS:
            sub = menu.addMenu(group)
            for t in types:
                sub.addAction(STEP_TYPES[t][0], lambda t=t: self.add_step(new_step(t), edit=True))
        add_btn.setMenu(menu)
        step_btns = self._buttons([("편집", self.edit_step), ("복제", self.dup_step), ("삭제", self.delete_step),
                                   ("▲", lambda: self.move_step(-1)), ("▼", lambda: self.move_step(1))])
        step_btns.insertWidget(0, add_btn)

        # 실행
        self.loop_spin = QSpinBox()
        self.loop_spin.setRange(0, 99999)
        self.loop_spin.setValue(1)
        self.loop_spin.setSpecialValueText("무한")
        self.run_btn = QPushButton("▶ 실행")
        self.run_btn.setStyleSheet("font-weight:bold; padding:6px 18px;")
        self.run_btn.clicked.connect(self.run_macro)
        self.stop_btn = QPushButton("■ 정지 (F8)")
        self.stop_btn.setShortcut(QKeySequence("F8"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_macro)
        run_row = QHBoxLayout()
        run_row.addWidget(QLabel("반복"))
        run_row.addWidget(self.loop_spin)
        run_row.addStretch()
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.stop_btn)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(2000)

        self.macro_label = QLabel("매크로: (없음) - [새 매크로] 또는 [열기]")
        self.macro_label.setStyleSheet("font-weight:bold;")
        right = QVBoxLayout()
        right.addWidget(self.macro_label)
        right.addWidget(QLabel("이미지 템플릿 (화면에서 드래그해서 추가)"))
        right.addWidget(self.tmpl_list)
        right.addLayout(tmpl_btns)
        right.addWidget(QLabel("스텝 (위에서부터 순서대로 실행, 드래그로 순서 변경)"))
        right.addWidget(self.step_list, 3)
        right.addLayout(step_btns)
        right.addLayout(run_row)
        right.addWidget(QLabel("로그"))
        right.addWidget(self.log_box, 2)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter()
        split.addWidget(self.screen)
        split.addWidget(right_w)
        split.setSizes([620, 660])
        self.setCentralWidget(split)

    def _act(self, tb, text, fn, shortcut=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(fn)
        tb.addAction(a)
        return a

    @staticmethod
    def _buttons(spec):
        row = QHBoxLayout()
        for text, fn in spec:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        return row

    def log(self, msg):
        self.log_box.appendPlainText(msg)

    def run_bg(self, fn, cb):
        def work():
            try:
                self._bg.done.emit(cb, fn(), None)
            except Exception as e:
                self._bg.done.emit(cb, None, e)
        threading.Thread(target=work, daemon=True).start()

    # ---------- 기기 ----------
    def refresh_devices(self):
        def done(devs, err):
            self.device_box.blockSignals(True)
            self.device_box.clear()
            if err:
                self.log(f"기기 조회 실패: {err}")
            for serial, state in devs or []:
                label = serial if state == "device" else f"{serial} ({state})"
                self.device_box.addItem(label, serial)
            self.device_box.blockSignals(False)
            if devs:
                self._device_changed()
                bad = [s for s, st in devs if st == "unauthorized"]
                if bad:
                    self.log("폰 화면에서 'USB 디버깅 허용'을 눌러주세요: " + ", ".join(bad))
            elif not err:
                self.log("연결된 기기가 없습니다. USB 디버깅을 켜고 연결하세요.")
        self.run_bg(self.adb.devices, done)

    def _device_changed(self):
        self.adb.serial = self.device_box.currentData()

    def wireless_connect(self):
        addr, ok = QInputDialog.getText(self, "무선 연결", "기기 IP:포트 (예: 192.168.0.10:5555)")
        if ok and addr.strip():
            self.run_bg(lambda: self.adb.connect(addr.strip()),
                        lambda r, e: (self.log(f"무선 연결: {e or r}"), self.refresh_devices()))

    def open_scrcpy(self):
        exe = find_tool("scrcpy")
        if not exe:
            QMessageBox.warning(self, "scrcpy", "scrcpy를 찾을 수 없습니다. tools 폴더를 확인하세요.")
            return
        cmd = [exe, "--window-title", "모두의 매크로 - 미러링", "--stay-awake"]
        if self.adb.serial:
            cmd += ["-s", self.adb.serial]
        subprocess.Popen(cmd, cwd=os.path.dirname(exe), creationflags=NO_WINDOW)
        self.log("scrcpy 미러링 창을 띄웠습니다 (마우스로 폰 조작 가능)")

    def capture(self):
        if not self.adb.serial:
            self.log("기기를 먼저 선택하세요")
            return

        def done(img, err):
            if err:
                self.log(f"캡처 실패: {err}")
                return
            self.screen.set_image(img)
            self.screen.set_boxes([])
            h, w = img.shape[:2]
            if self.macro and not self.macro.resolution:
                self.macro.resolution = [w, h]
                self.macro.save()
                self.log(f"매크로 기준 해상도 설정: {w}x{h}")
            elif self.macro and self.macro.resolution != [w, h]:
                self.log(f"주의: 매크로 기준 해상도 {self.macro.resolution} ≠ 현재 화면 {w}x{h} "
                         "(실행 시 자동 보정됨, 새 템플릿은 현재 화면 기준으로 잘림)")
        self.statusBar().showMessage("캡처 중...")
        self.run_bg(self.adb.screencap, done)

    # ---------- 매크로 파일 ----------
    def ensure_macro(self):
        return self.macro is not None or self.new_macro()

    def new_macro(self):
        name, ok = QInputDialog.getText(self, "새 매크로", "매크로 이름 (예: 원신_일일퀘)")
        name = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
        if not ok or not name:
            return False
        folder = os.path.join(MACRO_DIR, name)
        if os.path.exists(folder):
            QMessageBox.warning(self, "새 매크로", "같은 이름의 매크로가 이미 있습니다.")
            return False
        self.macro = Macro.create(folder)
        if self.screen.img is not None:
            h, w = self.screen.img.shape[:2]
            self.macro.resolution = [w, h]
            self.macro.save()
        self._load_macro_ui()
        return True

    def open_macro(self):
        folder = QFileDialog.getExistingDirectory(self, "매크로 폴더 선택", MACRO_DIR)
        if not folder:
            return
        if not os.path.exists(os.path.join(folder, "macro.json")):
            QMessageBox.warning(self, "열기", "macro.json이 있는 매크로 폴더를 선택하세요.")
            return
        try:
            self.macro = Macro.load(folder)
        except Exception as e:
            QMessageBox.critical(self, "열기 실패", str(e))
            return
        self._load_macro_ui()

    def save_macro(self):
        if self.macro:
            self.macro.save()
            self.log(f"저장됨: {self.macro.json_path}")

    def _load_macro_ui(self):
        res = "x".join(map(str, self.macro.resolution)) if self.macro.resolution else "미정"
        self.macro_label.setText(f"매크로: {self.macro.name}   (기준 해상도 {res})")
        self.refresh_templates()
        self.refresh_steps()
        self.log(f"매크로 열림: {self.macro.folder}")

    def edit_settings(self):
        if not self.ensure_macro():
            return
        st = self.macro.settings
        spec = [("step_delay", "스텝 사이 기본 대기 (ms)"), ("delay_jitter", "대기 랜덤 추가 최대 (ms)"),
                ("tap_jitter", "탭 좌표 랜덤 흔들림 (px)")]
        for key, label in spec:
            v, ok = QInputDialog.getInt(self, "실행 설정", label, int(st[key]), 0, 100000)
            if not ok:
                return
            st[key] = v
        self.macro.save()
        self.log(f"실행 설정 저장: {st}")

    # ---------- 템플릿 ----------
    def refresh_templates(self):
        self.tmpl_list.clear()
        if not self.macro:
            return
        for name in self.macro.template_names():
            item = QListWidgetItem(QIcon(self.macro.template_path(name)), name)
            item.setData(Qt.UserRole, name)
            self.tmpl_list.addItem(item)

    def current_template(self):
        item = self.tmpl_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def save_template(self, x, y, w, h):
        if not self.ensure_macro():
            return None
        name, ok = QInputDialog.getText(self, "템플릿 저장", "이미지 이름 (예: 시작버튼, 보상받기)")
        name = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
        if not ok or not name:
            return None
        path = self.macro.template_path(name)
        if os.path.exists(path) and QMessageBox.question(self, "덮어쓰기", f"'{name}' 을(를) 덮어쓸까요?") \
                != QMessageBox.Yes:
            return None
        vision.imwrite(path, self.screen.img[y:y + h, x:x + w].copy())
        self.refresh_templates()
        self.log(f"템플릿 저장: {name} ({w}x{h})")
        return name

    def test_template(self):
        name = self.current_template()
        if not name or self.screen.img is None:
            self.log("템플릿을 선택하고 화면을 캡처한 뒤 테스트하세요")
            return
        tmpl = vision.imread(self.macro.template_path(name))
        h, w = self.screen.img.shape[:2]
        scale = w / self.macro.resolution[0] if self.macro.resolution else 1.0
        score, box = vision.find(self.screen.img, tmpl, scale)
        if box is None:
            self.log(f"[{name}] 템플릿이 화면보다 큽니다")
            return
        color = QColor("#00e676") if score >= 0.85 else QColor("#ff5252")
        self.screen.set_boxes([(*box, color, f"{name} {score:.2f}")])
        verdict = "찾음" if score >= 0.85 else "못 찾음 (일치도를 낮추거나 이미지를 다시 잘라보세요)"
        self.log(f"[{name}] 일치도 {score:.3f} → {verdict}")

    def rename_template(self):
        name = self.current_template()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "이름 변경", "새 이름", text=name)
        new = re.sub(r'[\\/:*?"<>|]', "_", new.strip())
        if not ok or not new or new == name:
            return
        if os.path.exists(self.macro.template_path(new)):
            QMessageBox.warning(self, "이름 변경", "같은 이름이 이미 있습니다.")
            return
        os.rename(self.macro.template_path(name), self.macro.template_path(new))
        for s in self.macro.steps:
            if s.get("template") == name:
                s["template"] = new
        self.macro.save()
        self.refresh_templates()
        self.refresh_steps()

    def delete_template(self):
        name = self.current_template()
        if not name:
            return
        used = sum(1 for s in self.macro.steps if s.get("template") == name)
        msg = f"'{name}' 을(를) 삭제할까요?" + (f"\n(스텝 {used}개가 사용 중)" if used else "")
        if QMessageBox.question(self, "삭제", msg) == QMessageBox.Yes:
            os.remove(self.macro.template_path(name))
            self.refresh_templates()

    # ---------- 화면 클릭/드래그 ----------
    def _on_region(self, x, y, w, h):
        menu = QMenu(self)
        a_tap = menu.addAction("템플릿 저장 + '이미지 찾아서 탭' 스텝 추가")
        a_wait = menu.addAction("템플릿 저장 + '나타날 때까지 대기' 스텝 추가")
        a_if = menu.addAction("템플릿 저장 + '있으면 분기' 스텝 추가")
        a_only = menu.addAction("템플릿만 저장")
        chosen = menu.exec(QCursor.pos())
        if not chosen:
            return
        name = self.save_template(x, y, w, h)
        if not name:
            return
        kind = {a_tap: "tap_image", a_wait: "wait_image", a_if: "if_image"}.get(chosen)
        if kind:
            step = new_step(kind)
            step["template"] = name
            self.add_step(step, edit=kind == "if_image")

    def _on_point(self, x, y):
        menu = QMenu(self)
        a_tap = menu.addAction(f"({x}, {y}) 탭 스텝 추가")
        a_long = menu.addAction("길게 누르기 스텝 추가")
        if self.swipe_start:
            a_swipe = menu.addAction(f"스와이프 끝점 → 스텝 추가 (시작 {self.swipe_start})")
        else:
            a_swipe = menu.addAction("스와이프 시작점으로 지정")
        a_now = menu.addAction("지금 폰에서 바로 탭 (테스트)")
        chosen = menu.exec(QCursor.pos())
        if chosen == a_tap:
            self.add_step({**new_step("tap"), "x": x, "y": y})
        elif chosen == a_long:
            self.add_step({**new_step("long_press"), "x": x, "y": y})
        elif chosen == a_swipe:
            if self.swipe_start:
                x1, y1 = self.swipe_start
                self.add_step({**new_step("swipe"), "x1": x1, "y1": y1, "x2": x, "y2": y})
                self.swipe_start = None
            else:
                self.swipe_start = (x, y)
                self.log(f"스와이프 시작점 ({x}, {y}) - 끝점을 클릭하세요")
        elif chosen == a_now:
            self.run_bg(lambda: self.adb.tap(x, y),
                        lambda _, e: (self.log(f"탭 실패: {e}") if e else self.capture()))

    # ---------- 스텝 ----------
    def refresh_steps(self, select=None):
        self.step_list.clear()
        if not self.macro:
            return
        for i, s in enumerate(self.macro.steps):
            indent = "" if s["type"] == "label" else "    "
            item = QListWidgetItem(f"{i + 1:>3}. {indent}{describe(s)}")
            if s["type"] == "label":
                item.setForeground(QColor("#1565c0"))
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            elif s["type"] in ("goto", "stop"):
                item.setForeground(QColor("#8e24aa"))
            self.step_list.addItem(item)
        if select is not None and 0 <= select < self.step_list.count():
            self.step_list.setCurrentRow(select)

    def _steps_reordered(self, *_):
        # 드래그로 순서가 바뀌면 리스트 텍스트에서 원래 번호를 읽어 재배열
        order = [int(self.step_list.item(i).text().split(".")[0]) - 1 for i in range(self.step_list.count())]
        self.macro.steps = [self.macro.steps[i] for i in order]
        self.macro.save()
        row = self.step_list.currentRow()
        self.refresh_steps(row)

    def add_step(self, step, edit=False):
        if not self.ensure_macro():
            return
        if edit:
            dlg = StepDialog(step, self.macro.template_names(), self.macro.labels(), self)
            if not dlg.exec():
                return
            step = dlg.result_step()
        row = self.step_list.currentRow()
        idx = row + 1 if row >= 0 else len(self.macro.steps)
        self.macro.steps.insert(idx, step)
        self.macro.save()
        self.refresh_steps(idx)

    def edit_step(self):
        row = self.step_list.currentRow()
        if row < 0:
            return
        dlg = StepDialog(self.macro.steps[row], self.macro.template_names(), self.macro.labels(), self)
        if dlg.exec():
            self.macro.steps[row] = dlg.result_step()
            self.macro.save()
            self.refresh_steps(row)

    def dup_step(self):
        row = self.step_list.currentRow()
        if row >= 0:
            self.macro.steps.insert(row + 1, dict(self.macro.steps[row]))
            self.macro.save()
            self.refresh_steps(row + 1)

    def delete_step(self):
        row = self.step_list.currentRow()
        if row >= 0:
            del self.macro.steps[row]
            self.macro.save()
            self.refresh_steps(min(row, len(self.macro.steps) - 1))

    def move_step(self, d):
        row = self.step_list.currentRow()
        j = row + d
        if row < 0 or not 0 <= j < len(self.macro.steps):
            return
        st = self.macro.steps
        st[row], st[j] = st[j], st[row]
        self.macro.save()
        self.refresh_steps(j)

    # ---------- 실행 ----------
    def run_macro(self):
        if not self.macro or not self.macro.steps:
            self.log("실행할 스텝이 없습니다")
            return
        if not self.adb.serial:
            self.log("기기를 먼저 선택하세요")
            return
        problems = self._validate()
        if problems:
            QMessageBox.warning(self, "실행 불가", "\n".join(problems))
            return
        self.macro.save()
        self.runner = Runner(self.adb, self.macro, self.loop_spin.value())
        self.runner.log.connect(self.log)
        self.runner.step_changed.connect(self.step_list.setCurrentRow)
        self.runner.frame.connect(self._on_frame)
        self.runner.finished.connect(self._on_finished)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.log(f"▶ 실행: {self.macro.name}")
        self.runner.start()

    def _validate(self):
        names = set(self.macro.template_names())
        labels = self.macro.labels()
        out = []
        if len(labels) != len(set(labels)):
            out.append("같은 이름의 라벨이 여러 개 있습니다")
        for i, s in enumerate(self.macro.steps, 1):
            if "template" in s and s["template"] not in names:
                out.append(f"{i}번 스텝: 이미지를 선택하지 않았거나 없는 이미지입니다")
            if s["type"] == "goto" and not s["label"]:
                out.append(f"{i}번 스텝: 이동할 라벨을 선택하세요")
            for key in ("on_fail", "then", "else", "label"):
                v = s.get(key) if s["type"] != "label" else None
                if v and v not in ("stop", "next") and v not in labels:
                    out.append(f"{i}번 스텝: 라벨 '{v}' 이(가) 없습니다")
        return out

    def _on_frame(self, img, box):
        self.screen.set_image(img)
        self.screen.set_boxes([(*box, QColor("#00e676"), "")] if box else [])

    def _on_finished(self, msg):
        self.log(f"■ 종료: {msg}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def stop_macro(self):
        if self.runner:
            self.runner.stop()
            self.log("정지 요청...")

    def closeEvent(self, e):
        if self.runner and self.runner.is_running():
            self.runner.stop()
        if self.macro:
            self.macro.save()
        super().closeEvent(e)
