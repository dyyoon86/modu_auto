"""메인 창: 폰 화면 뷰(편집/녹화) + 템플릿 목록 + 스텝 목록 + 실행/로그."""
import os
import re
import subprocess
import threading
import time

import cv2
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QToolBar,
                               QVBoxLayout, QWidget)

from . import vision
from .adb import NO_WINDOW, ROOT, Adb, find_tool
from .dialogs import ConnectDialog, SettingsDialog
from .model import QUALITY_TEXT, RESERVED_TARGETS, STEP_GROUPS, STEP_TYPES, Macro, describe, new_step
from .runner import Runner
from .step_dialog import StepDialog

MACRO_DIR = os.path.join(ROOT, "macros")

QUALITY_COLOR = {"good": "#2e7d32", "warn": "#ef6c00", "bad": "#c62828"}
QUALITY_BG = {"good": "#e8f5e9", "warn": "#fff3e0", "bad": "#ffebee"}
QUALITY_ICON = {"good": "🟢", "warn": "🟡", "bad": "🔴"}

EDIT_HINT = ("✏️ 편집 모드 — 화면을 <b>드래그</b>하면 그 부분을 이미지로 등록, <b>클릭</b>하면 좌표 스텝 추가.  "
             "<b>[● 녹화]</b>를 켜면 터치만으로 매크로가 만들어져요.")
REC_HINT = ("🔴 <b>녹화 중</b> — 평소처럼 화면을 터치하세요. 폰에 그대로 전달되고 스텝이 자동으로 쌓여요.<br>"
            "클릭 = 탭 · 드래그 = 스와이프 · 꾹 누르기 = 길게 누르기.  "
            "화면이 바뀐 걸 확인하고 다음 터치를 하세요.")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s.strip())


def to_pixmap(img):
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QPixmap.fromImage(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())


class ScreenView(QWidget):
    """폰 화면을 비율 유지해 보여주고, 마우스 입력을 이미지(기기) 좌표로 알려준다.

    edit 모드: 클릭 / 드래그=영역 선택
    record 모드: 클릭 / 드래그=스와이프 / 꾹 누르기=길게 누르기
    """
    clicked = Signal(int, int)
    long_pressed = Signal(int, int, int)
    region_selected = Signal(int, int, int, int)
    swiped = Signal(int, int, int, int, int)
    hovered = Signal(int, int)

    LONG_PRESS_MS = 600

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 400)
        self.setMouseTracking(True)
        self.mode = "edit"
        self.img = None
        self.pix = None
        self.boxes = []    # 고정 표시: (x, y, w, h, QColor, text)
        self.flashes = []  # 잠깐 표시: (boxes, points)
        self._press = None
        self._press_t = 0.0
        self._cur = None

    def set_mode(self, mode):
        self.mode = mode
        self.update()

    def set_image(self, img):
        self.img = img
        self.pix = to_pixmap(img)
        self.update()

    def set_boxes(self, boxes):
        self.boxes = boxes
        self.update()

    def flash(self, boxes=(), points=(), ms=1500):
        item = (list(boxes), list(points))
        self.flashes.append(item)
        self.update()
        QTimer.singleShot(ms, lambda: self._unflash(item))

    def _unflash(self, item):
        self.flashes = [f for f in self.flashes if f is not item]
        self.update()

    def _target(self):
        if not self.pix:
            return QRect(), 1.0
        s = min(self.width() / self.pix.width(), self.height() / self.pix.height())
        w, h = int(self.pix.width() * s), int(self.pix.height() * s)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), s

    def _to_img(self, pos):
        r, s = self._target()
        x = int((pos.x() - r.x()) / s)
        y = int((pos.y() - r.y()) / s)
        return QPoint(max(0, min(self.pix.width() - 1, x)), max(0, min(self.pix.height() - 1, y)))

    def _to_widget(self, x, y):
        r, s = self._target()
        return QPoint(int(r.x() + x * s), int(r.y() + y * s))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        if not self.pix:
            p.setPen(QColor("#bbb"))
            p.setFont(QFont("Malgun Gothic", 11))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "① [기기 연결]로 폰을 연결하고\n② [실시간 보기]를 켜거나 [화면 캡처](F5)\n\n"
                       "③ [● 녹화]를 켜고 평소처럼 터치하면\n매크로가 자동으로 만들어져요")
            self._draw_border(p)
            return
        r, s = self._target()
        p.drawPixmap(r, self.pix)
        p.setFont(QFont("Malgun Gothic", 9))
        all_boxes = list(self.boxes)
        points = []
        for bs, ps in self.flashes:
            all_boxes += bs
            points += ps
        for x, y, w, h, color, text in all_boxes:
            p.setPen(QPen(color, 2))
            p.setBrush(Qt.NoBrush)
            tl = self._to_widget(x, y)
            p.drawRect(tl.x(), tl.y(), int(w * s), int(h * s))
            if text:
                p.drawText(tl.x(), tl.y() - 4, text)
        for x, y, color in points:
            c = self._to_widget(x, y)
            p.setPen(QPen(color, 3))
            p.drawEllipse(c, 14, 14)
            p.drawEllipse(c, 3, 3)
        if self._press and self._cur:
            if self.mode == "edit":
                p.setPen(QPen(QColor("#00e5ff"), 2, Qt.DashLine))
                p.drawRect(QRect(self._press, self._cur).normalized())
                self._draw_magnifier(p)
            elif (self._cur - self._press).manhattanLength() >= 6:
                p.setPen(QPen(QColor("#ff4081"), 4))
                p.drawLine(self._press, self._cur)
                p.drawEllipse(self._cur, 8, 8)
        self._draw_border(p)

    def _draw_border(self, p):
        if self.mode == "record":
            p.setPen(QPen(QColor("#e53935"), 6))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect().adjusted(3, 3, -3, -3))

    def _draw_magnifier(self, p):
        """드래그 중 커서 주변을 3배 확대해서 왼쪽 위에 보여준다."""
        c = self._to_img(self._cur)
        a = self._to_img(self._press)
        src = QRect(c.x() - 30, c.y() - 30, 60, 60)
        dst = QRect(10, 10, 180, 180)
        p.fillRect(dst, QColor("#000"))
        p.drawPixmap(dst, self.pix, src)
        p.setPen(QPen(QColor("#00e5ff"), 1))
        p.drawLine(dst.center().x(), dst.top(), dst.center().x(), dst.bottom())
        p.drawLine(dst.left(), dst.center().y(), dst.right(), dst.center().y())
        p.setPen(QPen(QColor("#fff"), 2))
        p.drawRect(dst)
        p.drawText(dst.left(), dst.bottom() + 16, f"{abs(c.x() - a.x())} x {abs(c.y() - a.y())} px")

    def mousePressEvent(self, e):
        if self.pix and e.button() == Qt.LeftButton:
            self._press = self._cur = e.position().toPoint()
            self._press_t = time.monotonic()

    def mouseMoveEvent(self, e):
        if not self.pix:
            return
        pos = e.position().toPoint()
        pt = self._to_img(pos)
        self.hovered.emit(pt.x(), pt.y())
        if self._press:
            self._cur = pos
            self.update()

    def mouseReleaseEvent(self, e):
        if not self._press or e.button() != Qt.LeftButton:
            return
        pos = e.position().toPoint()
        a, b = self._to_img(self._press), self._to_img(pos)
        moved = (pos - self._press).manhattanLength()
        ms = int((time.monotonic() - self._press_t) * 1000)
        self._press = self._cur = None
        self.update()
        if moved < 6:
            if ms >= self.LONG_PRESS_MS:
                self.long_pressed.emit(a.x(), a.y(), ms)
            else:
                self.clicked.emit(a.x(), a.y())
        elif self.mode == "record":
            self.swiped.emit(a.x(), a.y(), b.x(), b.y(), max(100, min(2000, ms)))
        else:
            x, y = min(a.x(), b.x()), min(a.y(), b.y())
            w, h = abs(a.x() - b.x()), abs(a.y() - b.y())
            if w >= 4 and h >= 4:
                self.region_selected.emit(x, y, w, h)


class LiveCapture(QObject):
    """켜져 있는 동안 계속 화면을 캡처해서 frame 시그널로 보낸다."""
    frame = Signal(object)
    error = Signal(str)

    def __init__(self, adb):
        super().__init__()
        self.adb = adb
        self._on = threading.Event()
        self._last_err = None
        threading.Thread(target=self._loop, daemon=True).start()

    def set_enabled(self, on):
        if on:
            self._on.set()
        else:
            self._on.clear()

    def _loop(self):
        while True:
            self._on.wait()
            if not self.adb.serial:
                time.sleep(0.5)
                continue
            try:
                img = self.adb.screencap()
                self._last_err = None
                if self._on.is_set():
                    self.frame.emit(img)
            except Exception as e:
                if str(e) != self._last_err:
                    self._last_err = str(e)
                    self.error.emit(str(e))
                time.sleep(1)


class _Bg(QObject):
    """백그라운드 작업 결과를 UI 스레드로 돌려주는 다리."""
    done = Signal(object, object, object)  # callback, result, error


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("모두의 매크로 - 게임 매크로 생성기")
        self.resize(1320, 860)
        os.makedirs(MACRO_DIR, exist_ok=True)
        self.adb = Adb()
        self.macro = None
        self.runner = None
        self.swipe_start = None
        self._warned_res = None
        self._devices = None
        self._wizard_shown = False
        self._bg = _Bg()
        self._bg.done.connect(lambda cb, res, err: cb(res, err))
        self.live = LiveCapture(self.adb)
        self.live.frame.connect(self._on_new_frame)
        self.live.error.connect(lambda m: self.log(f"실시간 캡처 오류: {m}"))
        self._build_ui()
        self.log("adb: " + (self.adb.path or "없음 (setup.bat을 실행하세요)"))
        self.dev_timer = QTimer(self)
        self.dev_timer.timeout.connect(self.refresh_devices)
        self.dev_timer.start(3000)
        self.refresh_devices()

    # ---------- UI 구성 ----------
    def _build_ui(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet("QToolButton { padding: 4px 8px; }")
        self.addToolBar(tb)
        tb.addWidget(QLabel(" 기기 "))
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(190)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        tb.addWidget(self.device_box)
        self._act(tb, "🔌 기기 연결", self.open_connect)
        self._act(tb, "scrcpy 미러링", self.open_scrcpy)
        tb.addSeparator()
        self.live_act = self._act(tb, "📺 실시간 보기", self.toggle_live)
        self.live_act.setCheckable(True)
        self.rec_act = self._act(tb, "● 녹화", self.toggle_record, "F9")
        self.rec_act.setCheckable(True)
        self._act(tb, "화면 캡처 (F5)", self.capture, "F5")
        tb.addSeparator()
        self._act(tb, "◀ 뒤로", lambda: self.send_key("BACK"))
        self._act(tb, "⌂ 홈", lambda: self.send_key("HOME"))
        tb.addSeparator()
        self._act(tb, "새 매크로", self.new_macro, "Ctrl+N")
        self._act(tb, "열기", self.open_macro, "Ctrl+O")
        self._act(tb, "저장", self.save_macro, "Ctrl+S")
        self._act(tb, "실행 설정", self.edit_settings)
        self._style_rec_button(False)

        # 왼쪽: 안내 배너 + 화면
        self.banner = QLabel(EDIT_HINT)
        self.banner.setWordWrap(True)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        self.feedback.setVisible(False)
        self.screen = ScreenView()
        self.screen.clicked.connect(self._on_click)
        self.screen.long_pressed.connect(self._on_long_press)
        self.screen.region_selected.connect(self._on_region)
        self.screen.swiped.connect(self._on_swipe)
        self.screen.hovered.connect(lambda x, y: self.statusBar().showMessage(f"좌표 ({x}, {y})"))
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(self.banner)
        left.addWidget(self.feedback)
        left.addWidget(self.screen, 1)
        left_w = QWidget()
        left_w.setLayout(left)
        self._set_banner(False)

        # 템플릿
        self.tmpl_list = QListWidget()
        self.tmpl_list.setViewMode(QListWidget.IconMode)
        self.tmpl_list.setIconSize(QSize(64, 64))
        self.tmpl_list.setGridSize(QSize(96, 96))
        self.tmpl_list.setResizeMode(QListWidget.Adjust)
        self.tmpl_list.setMovement(QListWidget.Static)
        self.tmpl_list.setMaximumHeight(170)
        self.tmpl_list.itemDoubleClicked.connect(lambda _: self.test_template())
        QShortcut(QKeySequence("F2"), self.tmpl_list, self.rename_template)
        tmpl_btns = self._buttons([("매칭 테스트", self.test_template), ("이름 변경 (F2)", self.rename_template),
                                   ("삭제", self.delete_template)])

        # 스텝
        self.step_list = QListWidget()
        self.step_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.step_list.model().rowsMoved.connect(lambda *_: QTimer.singleShot(0, self._steps_reordered))
        self.step_list.itemDoubleClicked.connect(lambda _: self.edit_step())
        QShortcut(QKeySequence("Delete"), self.step_list, self.delete_step)
        add_btn = QPushButton("스텝 추가 ▾")
        menu = QMenu(self)
        for group, types in STEP_GROUPS:
            sub = menu.addMenu(group)
            for t in types:
                sub.addAction(STEP_TYPES[t][0], lambda t=t: self.add_step(new_step(t), edit=bool(STEP_TYPES[t][1])))
        add_btn.setMenu(menu)
        step_btns = self._buttons([("편집", self.edit_step), ("복제", self.dup_step), ("삭제", self.delete_step),
                                   ("▲", lambda: self.move_step(-1)), ("▼", lambda: self.move_step(1))])
        step_btns.insertWidget(0, add_btn)

        # 실행
        self.loop_spin = QSpinBox()
        self.loop_spin.setRange(0, 99999)
        self.loop_spin.setValue(1)
        self.loop_spin.setSpecialValueText("무한")
        self.loop_spin.setSuffix(" 회")
        self.loop_spin.valueChanged.connect(self._loops_changed)
        self.run_btn = QPushButton("▶ 실행")
        self.run_btn.setStyleSheet("font-weight:bold; padding:6px 18px;")
        self.run_btn.clicked.connect(self.run_macro)
        self.stop_btn = QPushButton("■ 정지 (F8)")
        self.stop_btn.setShortcut(QKeySequence("F8"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_macro)
        run_row = QHBoxLayout()
        run_row.addWidget(QLabel("전체 반복"))
        run_row.addWidget(self.loop_spin)
        run_row.addWidget(QLabel("(0 = 무한)"))
        run_row.addStretch()
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.stop_btn)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(2000)

        self.macro_label = QLabel("매크로: (없음) — [● 녹화]를 누르면 자동으로 만들어져요")
        self.macro_label.setStyleSheet("font-weight:bold;")
        right = QVBoxLayout()
        right.addWidget(self.macro_label)
        right.addWidget(QLabel("이미지 (🟢좋음 🟡주의 🔴나쁨 · 더블클릭 = 매칭 테스트)"))
        right.addWidget(self.tmpl_list)
        right.addLayout(tmpl_btns)
        right.addWidget(QLabel("스텝 (위에서부터 순서대로 실행 · 드래그로 순서 변경 · 더블클릭 = 편집)"))
        right.addWidget(self.step_list, 3)
        right.addLayout(step_btns)
        right.addLayout(run_row)
        right.addWidget(QLabel("로그"))
        right.addWidget(self.log_box, 2)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter()
        split.addWidget(left_w)
        split.addWidget(right_w)
        split.setSizes([640, 680])
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

    def _set_banner(self, recording):
        self.banner.setText(REC_HINT if recording else EDIT_HINT)
        bg, fg = ("#ffebee", "#b71c1c") if recording else ("#e3f2fd", "#0d47a1")
        self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:8px; border-radius:4px;")

    def _style_rec_button(self, on):
        w = self.findChild(QToolBar).widgetForAction(self.rec_act)
        if w:
            w.setStyleSheet("color:white; background:#e53935; font-weight:bold; border-radius:3px;" if on
                            else "color:#e53935; font-weight:bold;")

    def show_feedback(self, name, level, msg):
        self.feedback.setText(f"방금 등록: <b>{name}</b> — {QUALITY_ICON[level]} {msg}")
        self.feedback.setStyleSheet(f"background:{QUALITY_BG[level]}; color:{QUALITY_COLOR[level]};"
                                    "padding:6px; border-radius:4px;")
        self.feedback.setVisible(True)

    def log(self, msg):
        self.log_box.appendPlainText(msg)

    def run_bg(self, fn, cb=None):
        cb = cb or (lambda _, e: e and self.log(f"오류: {e}"))

        def work():
            try:
                self._bg.done.emit(cb, fn(), None)
            except Exception as e:
                self._bg.done.emit(cb, None, e)
        threading.Thread(target=work, daemon=True).start()

    # ---------- 기기 ----------
    def refresh_devices(self):
        def done(devs, err):
            if err:
                if self._devices is None:
                    self.log(f"기기 조회 실패: {err}")
                self._devices = []
                return
            if devs == self._devices:
                return
            first = self._devices is None
            self._devices = devs
            current = self.adb.serial
            self.device_box.blockSignals(True)
            self.device_box.clear()
            for serial, state in devs:
                self.device_box.addItem(serial if state == "device" else f"{serial} ({state})", serial)
            idx = self.device_box.findData(current)
            ready = [i for i, (_, st) in enumerate(devs) if st == "device"]
            self.device_box.setCurrentIndex(idx if idx >= 0 else (ready[0] if ready else 0))
            self.device_box.blockSignals(False)
            self._device_changed()
            if ready:
                self.log("기기 연결됨: " + ", ".join(s for s, st in devs if st == "device"))
            elif first and not self._wizard_shown:
                self._wizard_shown = True
                QTimer.singleShot(300, self.open_connect)
        self.run_bg(self.adb.devices, done)

    def _device_changed(self):
        self.adb.serial = self.device_box.currentData()

    def open_connect(self):
        self._wizard_shown = True
        ConnectDialog(self.adb, self).exec()
        self._devices = None
        self.refresh_devices()

    def open_scrcpy(self):
        exe = find_tool("scrcpy")
        if not exe:
            QMessageBox.warning(self, "scrcpy", "scrcpy를 찾을 수 없습니다. setup.bat을 실행하세요.")
            return
        cmd = [exe, "--window-title", "모두의 매크로 - 미러링", "--stay-awake"]
        if self.adb.serial:
            cmd += ["-s", self.adb.serial]
        subprocess.Popen(cmd, cwd=os.path.dirname(exe), creationflags=NO_WINDOW)
        self.log("scrcpy 미러링 창을 띄웠습니다")

    def _need_device(self):
        if self.adb.serial and any(s == self.adb.serial and st == "device" for s, st in self._devices or []):
            return True
        self.log("기기가 연결되지 않았어요 — [🔌 기기 연결]을 눌러주세요")
        return False

    def capture(self):
        if not self._need_device():
            return
        self.statusBar().showMessage("캡처 중...")
        self.run_bg(self.adb.screencap,
                    lambda img, err: self.log(f"캡처 실패: {err}") if err else self._on_new_frame(img, True))

    def toggle_live(self):
        on = self.live_act.isChecked()
        if on and not self._need_device():
            self.live_act.setChecked(False)
            return
        self.live.set_enabled(on)

    def _on_new_frame(self, img, clear_boxes=False):
        self.screen.set_image(img)
        if clear_boxes:
            self.screen.set_boxes([])
        h, w = img.shape[:2]
        if not self.macro:
            return
        if not self.macro.resolution:
            self.macro.resolution = [w, h]
            self.macro.save()
            self._update_macro_label()
            self.log(f"매크로 기준 해상도: {w}x{h}")
        elif self.macro.resolution != [w, h] and self._warned_res != (w, h):
            self._warned_res = (w, h)
            self.log(f"⚠️ 매크로 기준 해상도 {self.macro.resolution} ≠ 현재 화면 {w}x{h} "
                     "(화면이 회전했거나 다른 기기). 실행은 자동 보정되지만 새 이미지는 기준 화면에서 등록하세요.")

    def send_key(self, key):
        if not self._need_device():
            return
        self.run_bg(lambda: self.adb.key(key))
        if self.rec_act.isChecked() and self.macro:
            self.add_step({**new_step("key"), "key": key})
            self.log(f"녹화: 버튼 {key}")

    # ---------- 녹화 ----------
    def toggle_record(self):
        on = self.rec_act.isChecked()
        if on:
            if not self._need_device() or not self.ensure_macro():
                self.rec_act.setChecked(False)
                return
            self._live_before = self.live_act.isChecked()
            self.live_act.setChecked(True)
            self.live.set_enabled(True)
            self.step_list.setCurrentRow(self.step_list.count() - 1)  # 끝에 이어서 녹화
            self.log("● 녹화 시작")
        else:
            self.live_act.setChecked(getattr(self, "_live_before", False))
            self.live.set_enabled(self.live_act.isChecked())
            self.log("■ 녹화 끝 — ▶ 실행으로 확인해 보세요")
        self.live_act.setEnabled(not on)
        self.screen.set_mode("record" if on else "edit")
        self._set_banner(on)
        self._style_rec_button(on)

    @staticmethod
    def _judge(level, msg, second):
        """품질 결과에 맞는 일치도 기준을 정하고 안내 문구를 붙인다. → (level, msg, threshold)"""
        th = vision.LOOKALIKE
        if level == "warn" and second >= vision.LOOKALIKE:
            th = vision.suggest_threshold(second)
            if second + 0.03 <= vision.MAX_THRESHOLD:
                msg += f" → 일치도 기준을 {th:.2f}로 올려서 구분하게 했어요"
            else:
                msg += (" → 똑같이 생겨서 이 자리에서 가장 가까운 것을 누르게 했어요."
                        " 화면 배치가 바뀌는 게임이면 다른 부분을 잘라보세요")
        return level, msg, th

    def record_tap(self, x, y):
        img = self.screen.img
        box, level, msg, second = vision.auto_crop(img, x, y)
        level, msg, th = self._judge(level, msg, second)
        name = self.macro.next_template_name()
        self._write_template(name, img, box, level, msg)
        bx, by, bw, bh = box
        step = new_step("tap_image")
        step.update(template=name, threshold=th, hint_x=bx + bw // 2, hint_y=by + bh // 2,
                    offset_x=int(x - (bx + bw // 2)), offset_y=int(y - (by + bh // 2)))
        self.add_step(step)
        self.screen.flash([(*box, QColor(QUALITY_COLOR[level]), name)], [(x, y, QColor("#ff4081"))])
        self.run_bg(lambda: self.adb.tap(x, y))
        self.show_feedback(name, level, msg)
        self.log(f"녹화: [{name}] 탭 ({x},{y}) — {QUALITY_TEXT[level]}: {msg}")

    # ---------- 화면 마우스 입력 ----------
    def _on_click(self, x, y):
        if self.screen.mode == "record":
            self.record_tap(x, y)
        else:
            self._point_menu(x, y)

    def _on_long_press(self, x, y, ms):
        if self.screen.mode != "record":
            self._point_menu(x, y)
            return
        ms = max(600, ms)
        self.add_step({**new_step("long_press"), "x": x, "y": y, "ms": ms})
        self.screen.flash(points=[(x, y, QColor("#ff9800"))])
        self.run_bg(lambda: self.adb.swipe(x, y, x, y, ms))
        self.log(f"녹화: 길게 누르기 ({x},{y}) {ms}ms")

    def _on_swipe(self, x1, y1, x2, y2, ms):
        self.add_step({**new_step("swipe"), "x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms})
        self.screen.flash(points=[(x1, y1, QColor("#ff4081")), (x2, y2, QColor("#ff4081"))])
        self.run_bg(lambda: self.adb.swipe(x1, y1, x2, y2, ms))
        self.log(f"녹화: 스와이프 ({x1},{y1}) → ({x2},{y2}) {ms}ms")

    def _on_region(self, x, y, w, h):
        menu = QMenu(self)
        actions = {
            menu.addAction("이미지 등록 + '찾아서 탭' 스텝"): "tap_image",
            menu.addAction("이미지 등록 + '나타날 때까지 대기' 스텝"): "wait_image",
            menu.addAction("이미지 등록 + '사라질 때까지 대기' 스텝"): "wait_gone",
            menu.addAction("이미지만 등록"): None,
        }
        chosen = menu.exec(QCursor.pos())
        if chosen not in actions or not self.ensure_macro():
            return
        name, th = self.save_template((x, y, w, h))
        kind = actions[chosen]
        if kind:
            self.add_step({**new_step(kind), "template": name, "threshold": th,
                           "hint_x": x + w // 2, "hint_y": y + h // 2})

    def _point_menu(self, x, y):
        menu = QMenu(self)
        a_tap = menu.addAction(f"({x}, {y}) 탭 스텝 추가")
        a_long = menu.addAction("길게 누르기 스텝 추가")
        a_swipe = menu.addAction(f"스와이프 끝점 → 스텝 추가 (시작 {self.swipe_start})" if self.swipe_start
                                 else "스와이프 시작점으로 지정")
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
                self.log(f"스와이프 시작점 ({x}, {y}) — 끝점을 클릭하세요")
        elif chosen == a_now and self._need_device():
            self.run_bg(lambda: self.adb.tap(x, y))

    # ---------- 매크로 파일 ----------
    def ensure_macro(self):
        return self.macro is not None or self.new_macro()

    def new_macro(self):
        default = time.strftime("매크로_%m%d_%H%M")
        name, ok = QInputDialog.getText(self, "새 매크로", "매크로 이름 (나중에 폴더 이름이 돼요)", text=default)
        name = safe_name(name)
        if not ok or not name:
            return False
        folder = os.path.join(MACRO_DIR, name)
        if os.path.exists(folder):
            QMessageBox.warning(self, "새 매크로", "같은 이름의 매크로가 이미 있어요.")
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

    def _update_macro_label(self):
        res = "x".join(map(str, self.macro.resolution)) if self.macro.resolution else "미정"
        self.macro_label.setText(f"매크로: {self.macro.name}   (기준 해상도 {res})")

    def _load_macro_ui(self):
        self._warned_res = None
        self._update_macro_label()
        self.loop_spin.blockSignals(True)
        self.loop_spin.setValue(int(self.macro.settings.get("loops", 1)))
        self.loop_spin.blockSignals(False)
        self.refresh_templates()
        self.refresh_steps()
        self.log(f"매크로 열림: {self.macro.folder}")

    def _loops_changed(self, v):
        if self.macro:
            self.macro.settings["loops"] = v
            self.macro.save()

    def edit_settings(self):
        if not self.ensure_macro():
            return
        dlg = SettingsDialog(self.macro.settings, self)
        if dlg.exec():
            self.macro.settings = dlg.result_settings()
            self.macro.save()
            self.log("실행 설정 저장됨")

    # ---------- 템플릿 ----------
    def refresh_templates(self):
        self.tmpl_list.clear()
        if not self.macro:
            return
        for name in self.macro.template_names():
            info = self.macro.tmpl_info.get(name, {})
            level = info.get("level")
            item = QListWidgetItem(QIcon(self.macro.template_path(name)),
                                   f"{QUALITY_ICON[level]} {name}" if level else name)
            item.setData(Qt.UserRole, name)
            if level:
                item.setBackground(QColor(QUALITY_BG[level]))
                item.setToolTip(f"{QUALITY_TEXT[level]}: {info.get('msg', '')}")
            self.tmpl_list.addItem(item)

    def current_template(self):
        item = self.tmpl_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _write_template(self, name, img, box, level, msg):
        x, y, w, h = box
        vision.imwrite(self.macro.template_path(name), img[y:y + h, x:x + w].copy())
        self.macro.tmpl_info[name] = {"level": level, "msg": msg}
        self.macro.save()
        self.refresh_templates()

    def save_template(self, box):
        """화면의 box 영역을 자동 이름으로 저장하고 품질을 알려준다. → (이름, 권장 일치도)"""
        img = self.screen.img
        level, msg, th = self._judge(*vision.quality(img, box))
        name = self.macro.next_template_name()
        self._write_template(name, img, box, level, msg)
        self.screen.flash([(*box, QColor(QUALITY_COLOR[level]), name)])
        self.show_feedback(name, level, msg)
        self.log(f"이미지 등록: {name} ({box[2]}x{box[3]}) — {QUALITY_TEXT[level]}: {msg}")
        return name, th

    def test_template(self):
        name = self.current_template()
        if not name or self.screen.img is None:
            self.log("이미지를 선택하고 화면을 띄운 뒤 테스트하세요")
            return
        tmpl = vision.imread(self.macro.template_path(name))
        h, w = self.screen.img.shape[:2]
        scale = w / self.macro.resolution[0] if self.macro.resolution else 1.0
        score, box = vision.find(self.screen.img, tmpl, scale)
        if box is None:
            self.log(f"[{name}] 이미지가 화면보다 커요")
            return
        color = QColor("#00e676") if score >= 0.85 else QColor("#ff5252")
        self.screen.set_boxes([(*box, color, f"{name} {score:.2f}")])
        verdict = "찾음" if score >= 0.85 else "못 찾음 (이 화면에 없거나, 다시 잘라보세요)"
        self.log(f"[{name}] 일치도 {score:.3f} → {verdict}")

    def rename_template(self):
        name = self.current_template()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "이름 변경", "새 이름 (예: 시작버튼, 보상받기)", text=name)
        new = safe_name(new)
        if not ok or not new or new == name:
            return
        if os.path.exists(self.macro.template_path(new)):
            QMessageBox.warning(self, "이름 변경", "같은 이름이 이미 있어요.")
            return
        os.rename(self.macro.template_path(name), self.macro.template_path(new))
        if name in self.macro.tmpl_info:
            self.macro.tmpl_info[new] = self.macro.tmpl_info.pop(name)
        for s in self.macro.steps:
            if s.get("template") == name:
                s["template"] = new
        self.macro.save()
        self.refresh_templates()
        self.refresh_steps(self.step_list.currentRow())

    def delete_template(self):
        name = self.current_template()
        if not name:
            return
        used = sum(1 for s in self.macro.steps if s.get("template") == name)
        msg = f"'{name}' 을(를) 삭제할까요?" + (f"\n(스텝 {used}개가 사용 중)" if used else "")
        if QMessageBox.question(self, "삭제", msg) == QMessageBox.Yes:
            os.remove(self.macro.template_path(name))
            self.macro.save()
            self.refresh_templates()

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
            elif s["type"] in ("goto", "stop", "end_round"):
                item.setForeground(QColor("#8e24aa"))
            self.step_list.addItem(item)
        if select is not None and 0 <= select < self.step_list.count():
            self.step_list.setCurrentRow(select)
            self.step_list.scrollToItem(self.step_list.currentItem())

    def _steps_reordered(self):
        # 드래그로 순서가 바뀌면 리스트 텍스트에서 원래 번호를 읽어 재배열
        order = [int(self.step_list.item(i).text().split(".")[0]) - 1 for i in range(self.step_list.count())]
        self.macro.steps = [self.macro.steps[i] for i in order]
        self.macro.save()
        self.refresh_steps(self.step_list.currentRow())

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
            self.log("실행할 스텝이 없어요")
            return
        if not self._need_device():
            return
        problems = self._validate()
        if problems:
            QMessageBox.warning(self, "실행 불가", "\n".join(problems))
            return
        if self.rec_act.isChecked():
            self.rec_act.setChecked(False)
            self.toggle_record()
        self._live_during_run = self.live_act.isChecked()
        self.live.set_enabled(False)  # 실행 중에는 엔진이 화면을 보내준다
        self.macro.save()
        self.runner = Runner(self.adb, self.macro, self.loop_spin.value())
        self.runner.log.connect(self.log)
        self.runner.step_changed.connect(self.step_list.setCurrentRow)
        self.runner.frame.connect(self._on_frame)
        self.runner.finished.connect(self._on_finished)
        self.run_btn.setEnabled(False)
        self.rec_act.setEnabled(False)
        self.stop_btn.setEnabled(True)
        loops = self.loop_spin.value()
        self.log(f"▶ 실행: {self.macro.name} ({'무한 반복' if loops == 0 else f'{loops}회'})")
        self.runner.start()

    def _validate(self):
        names = set(self.macro.template_names())
        labels = self.macro.labels()
        out = []
        if len(labels) != len(set(labels)):
            out.append("같은 이름의 라벨이 여러 개 있어요")
        for i, s in enumerate(self.macro.steps, 1):
            if s["type"] == "label" and s["name"] in RESERVED_TARGETS:
                out.append(f"{i}번 스텝: 라벨 이름으로 '{s['name']}'은(는) 쓸 수 없어요")
            if "template" in s and s["template"] not in names:
                out.append(f"{i}번 스텝: 이미지를 선택하지 않았거나 없는 이미지예요")
            if s["type"] == "goto" and not s["label"]:
                out.append(f"{i}번 스텝: 이동할 라벨을 선택하세요")
            for key in ("on_fail", "then", "else", "label"):
                v = s.get(key) if s["type"] != "label" else None
                if v and v not in RESERVED_TARGETS and v not in labels:
                    out.append(f"{i}번 스텝: 라벨 '{v}' 이(가) 없어요")
        return out

    def _on_frame(self, img, box):
        self.screen.set_image(img)
        self.screen.set_boxes([(*box, QColor("#00e676"), "")] if box else [])

    def _on_finished(self, msg):
        self.log(f"■ 종료: {msg}")
        self.run_btn.setEnabled(True)
        self.rec_act.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.screen.set_boxes([])
        self.live.set_enabled(getattr(self, "_live_during_run", False))

    def stop_macro(self):
        if self.runner:
            self.runner.stop()
            self.log("정지 요청...")

    def closeEvent(self, e):
        self.live.set_enabled(False)
        if self.runner and self.runner.is_running():
            self.runner.stop()
        if self.macro:
            self.macro.save()
        super().closeEvent(e)
