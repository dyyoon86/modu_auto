"""기기 연결 도우미, 실행 설정 대화상자."""
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

# 에뮬레이터들이 adb용으로 여는 대표 포트
EMULATOR_PORTS = [5555, 5557, 5559, 5565, 5575, 7555, 16384, 16416, 62001, 62025, 21503]

USB_GUIDE = """
<ol style="line-height:160%">
<li><b>개발자 모드 켜기</b><br>폰 <b>설정 → 휴대전화 정보 → 소프트웨어 정보</b>에서
<b>'빌드번호'</b>를 7번 연속 톡톡톡 누르세요.<br>"개발자 모드가 켜졌습니다"가 나오면 성공.</li>
<li><b>USB 디버깅 켜기</b><br><b>설정 → 개발자 옵션 → 'USB 디버깅'</b>을 켜세요.</li>
<li><b>PC에 USB로 연결</b><br>폰 화면에 <b>'USB 디버깅을 허용하시겠습니까?'</b>가 뜨면
<b>'이 컴퓨터에서 항상 허용'</b>을 체크하고 <b>허용</b>.</li>
</ol>
<p><b>그래도 안 잡히면</b><br>
· 충전 전용 케이블일 수 있어요 — 데이터 케이블로 바꿔보세요<br>
· 삼성폰은 PC에 'Samsung USB Driver'가 필요할 수 있어요<br>
· 폰 상단 알림에서 USB 모드를 '파일 전송'으로 바꿔보세요</p>
"""

EMU_GUIDE = """
<p>에뮬레이터 설정에서 <b>ADB 연결</b>을 먼저 켜주세요.</p>
<ul style="line-height:160%">
<li><b>LDPlayer</b>: 설정 → 기타 설정 → ADB 디버깅 → '로컬 연결 열기'</li>
<li><b>블루스택 5</b>: 설정 → 고급 → 'Android 디버그 브리지(ADB)' 켜기</li>
<li><b>MuMu / 녹스</b>: 기본으로 켜져 있어요</li>
</ul>
<p>그다음 아래 버튼을 누르면 자동으로 찾아서 연결합니다.</p>
"""

WIFI_GUIDE = """
<p><b>안드로이드 11 이상</b>, PC와 폰이 <b>같은 와이파이</b>여야 해요.</p>
<ol style="line-height:160%">
<li>개발자 옵션 → <b>무선 디버깅</b> 켜기 → <b>'페어링 코드로 기기 페어링'</b></li>
<li>폰에 나온 <b>IP주소:포트</b>와 <b>6자리 코드</b>를 아래 1번에 입력 → [페어링]</li>
<li>무선 디버깅 첫 화면의 <b>IP주소:포트</b>(페어링 포트와 다름)를 2번에 입력 → [연결]</li>
</ol>
"""


class _Sig(QObject):
    status = Signal(str, str)  # 텍스트, 색
    log = Signal(str)


class ConnectDialog(QDialog):
    """연결 상태를 실시간으로 보여주며 USB/에뮬레이터/무선 연결을 안내한다."""

    def __init__(self, adb, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("기기 연결 도우미")
        self.resize(560, 600)
        self._busy = False
        self.sig = _Sig()
        self.sig.status.connect(self._set_status)
        self.sig.log.connect(lambda m: self.log.appendPlainText(m))

        lay = QVBoxLayout(self)
        self.status = QLabel("연결 상태 확인 중...")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:15px; font-weight:bold; padding:12px; border-radius:6px;"
                                  "background:#eceff1;")
        lay.addWidget(self.status)

        tabs = QTabWidget()
        tabs.addTab(self._page(USB_GUIDE), "USB (폰)")
        emu = self._page(EMU_GUIDE)
        b = QPushButton("에뮬레이터 자동 찾기")
        b.clicked.connect(self.find_emulators)
        emu.layout().addWidget(b)
        tabs.addTab(emu, "PC 에뮬레이터")
        wifi = self._page(WIFI_GUIDE)
        self.pair_addr, self.pair_code, self.conn_addr = QLineEdit(), QLineEdit(), QLineEdit()
        self.pair_addr.setPlaceholderText("192.168.0.10:37123")
        self.pair_code.setPlaceholderText("123456")
        self.conn_addr.setPlaceholderText("192.168.0.10:41234")
        form = QFormLayout()
        row1 = QHBoxLayout()
        row1.addWidget(self.pair_addr, 2)
        row1.addWidget(self.pair_code, 1)
        pb = QPushButton("페어링")
        pb.clicked.connect(self.pair)
        row1.addWidget(pb)
        row2 = QHBoxLayout()
        row2.addWidget(self.conn_addr)
        cb = QPushButton("연결")
        cb.clicked.connect(lambda: self._connect_addr(self.conn_addr.text().strip()))
        row2.addWidget(cb)
        form.addRow("1. 페어링", row1)
        form.addRow("2. 연결", row2)
        wifi.layout().addLayout(form)
        tabs.addTab(wifi, "무선 (Wi-Fi)")
        lay.addWidget(tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        lay.addWidget(self.log)
        close = QPushButton("완료")
        close.clicked.connect(self.accept)
        lay.addWidget(close, alignment=Qt.AlignRight)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1500)
        self.poll()

    @staticmethod
    def _page(html):
        w = QWidget()
        v = QVBoxLayout(w)
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        v.addWidget(lbl)
        v.addStretch()
        return w

    def _bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _set_status(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet("font-size:15px; font-weight:bold; padding:12px; border-radius:6px;"
                                  f"background:{color};")

    def poll(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                devs = self.adb.devices()
                ok = [s for s, st in devs if st == "device"]
                if ok:
                    names = []
                    for s in ok:
                        try:
                            names.append(f"{self.adb.model(s) or '기기'} ({s})")
                        except Exception:
                            names.append(s)
                    self.sig.status.emit("✅ 연결됨: " + ", ".join(names) + "\n[완료]를 누르세요.", "#c8e6c9")
                elif any(st == "unauthorized" for _, st in devs):
                    self.sig.status.emit("⚠️ 폰 화면에 뜬 'USB 디버깅 허용'을 눌러주세요", "#fff3c4")
                elif devs:
                    self.sig.status.emit("⚠️ 기기 상태: " + ", ".join(st for _, st in devs) +
                                         " — 케이블을 다시 꽂아보세요", "#fff3c4")
                else:
                    self.sig.status.emit("❌ 연결된 기기가 없어요 — 아래 안내를 따라해 주세요", "#ffcdd2")
            except Exception as e:
                self.sig.status.emit(f"❌ adb 오류: {e}", "#ffcdd2")
            finally:
                self._busy = False
        self._bg(work)

    def find_emulators(self):
        self.sig.log.emit("에뮬레이터 찾는 중...")

        def work():
            found = 0
            for port in EMULATOR_PORTS:
                try:
                    self.adb.connect(f"127.0.0.1:{port}")
                    self.sig.log.emit(f"✅ 127.0.0.1:{port} 연결됨")
                    found += 1
                except Exception:
                    pass
            if not found:
                self.sig.log.emit("못 찾았어요. 에뮬레이터가 켜져 있고 ADB 설정이 켜져 있는지 확인하세요.")
        self._bg(work)

    def pair(self):
        addr, code = self.pair_addr.text().strip(), self.pair_code.text().strip()
        if not addr or not code:
            self.sig.log.emit("페어링 IP:포트와 코드를 입력하세요")
            return

        def work():
            try:
                self.sig.log.emit("페어링: " + self.adb.pair(addr, code))
            except Exception as e:
                self.sig.log.emit(f"페어링 실패: {e}")
        self._bg(work)

    def _connect_addr(self, addr):
        if not addr:
            self.sig.log.emit("연결할 IP:포트를 입력하세요")
            return

        def work():
            try:
                self.sig.log.emit("✅ " + self.adb.connect(addr))
            except Exception as e:
                self.sig.log.emit(f"연결 실패: {e}")
        self._bg(work)

    def done(self, r):
        self.timer.stop()
        super().done(r)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실행 설정")
        self.settings = dict(settings)
        form = QFormLayout(self)
        self.w = {}
        spec = [
            ("step_delay", "스텝 사이 기본 대기 (ms)", 0, 60000),
            ("delay_jitter", "대기에 더할 랜덤 최대 (ms)", 0, 60000),
            ("tap_jitter", "탭 위치 랜덤 흔들림 (px)", 0, 100),
        ]
        for key, label, lo, hi in spec:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setValue(int(self.settings[key]))
            form.addRow(label, sb)
            self.w[key] = sb
        poll = QDoubleSpinBox()
        poll.setRange(0.05, 10)
        poll.setSingleStep(0.1)
        poll.setValue(float(self.settings["poll"]))
        form.addRow("이미지 재확인 간격 (초)", poll)
        self.w["poll"] = poll
        hint = QLabel("랜덤 값은 매번 조금씩 다르게 움직여서 사람이 하는 것처럼 보이게 합니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        form.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def result_settings(self):
        for key, w in self.w.items():
            self.settings[key] = w.value()
        return self.settings
