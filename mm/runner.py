"""매크로 실행 엔진. 별도 스레드에서 돌고 Qt 시그널로 진행상황을 알린다."""
import random
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import vision


class _Stopped(Exception):
    pass


class _Finish(Exception):
    pass


class Runner(QObject):
    log = Signal(str)
    step_changed = Signal(int)
    frame = Signal(object, object)  # (화면 이미지, 찾은 박스 or None)
    finished = Signal(str)

    def __init__(self, adb, macro, loops=1):
        super().__init__()
        self.adb = adb
        self.macro = macro
        self.loops = loops  # 0 = 무한
        self.cfg = macro.settings
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---- 내부 유틸 ----
    def _sleep(self, sec):
        if self._stop.wait(max(0.0, sec)):
            raise _Stopped()

    def _capture(self, box=None):
        img = self.adb.screencap()
        self.frame.emit(img, box)
        return img

    def _pt(self, x, y):
        """매크로 해상도 좌표 → 현재 기기 좌표 (+랜덤 흔들림)."""
        j = int(self.cfg.get("tap_jitter", 0))
        return (x * self.sx + random.randint(-j, j), y * self.sy + random.randint(-j, j))

    def _search(self, name, threshold, timeout, want_present=True):
        """timeout 동안 이미지를 찾는다. want_present=False면 사라질 때까지."""
        tmpl = self.templates.get(name)
        if tmpl is None:
            raise RuntimeError(f"템플릿 '{name}' 이(가) 없습니다")
        deadline = time.time() + timeout
        while True:
            screen = self.adb.screencap()
            score, box = vision.find(screen, tmpl, self.sx)
            found = score >= threshold
            self.frame.emit(screen, box if found else None)
            if found == want_present:
                return box, score
            if time.time() >= deadline:
                return None, score
            self._sleep(float(self.cfg.get("poll", 0.4)))

    def _jump(self, target, i):
        if target in ("", "next"):
            return i + 1
        if target == "stop":
            raise _Finish()
        if target not in self.label_idx:
            raise RuntimeError(f"라벨 '{target}' 이(가) 없습니다")
        return self.label_idx[target]

    # ---- 메인 루프 ----
    def _main(self):
        try:
            self._prepare()
            run = 0
            while self.loops == 0 or run < self.loops:
                run += 1
                self.log.emit(f"── {run}회차 시작" + ("" if self.loops == 0 else f" / {self.loops}"))
                try:
                    self._run_once()
                except _Finish:
                    self.log.emit("종료 스텝 도달")
            self.finished.emit("완료")
        except _Stopped:
            self.finished.emit("사용자가 중지함")
        except Exception as e:  # 기기 끊김 등
            self.finished.emit(f"오류: {e}")

    def _prepare(self):
        m = self.macro
        self.templates = {}
        for name in m.template_names():
            img = vision.imread(m.template_path(name))
            if img is not None:
                self.templates[name] = img
        screen = self._capture()
        h, w = screen.shape[:2]
        if m.resolution:
            bw, bh = m.resolution
            self.sx, self.sy = w / bw, h / bh
            if abs(self.sx - 1) > 0.01 or abs(self.sy - 1) > 0.01:
                self.log.emit(f"해상도 보정: 매크로 {bw}x{bh} → 기기 {w}x{h}")
        else:
            self.sx = self.sy = 1.0
        self.label_idx = {s["name"]: i for i, s in enumerate(m.steps) if s["type"] == "label"}

    def _run_once(self):
        steps = self.macro.steps
        goto_count = {}
        i = 0
        while i < len(steps):
            if self._stop.is_set():
                raise _Stopped()
            s = steps[i]
            self.step_changed.emit(i)
            i = self._exec(s, i, goto_count)
            if s["type"] not in ("label", "goto", "wait"):
                delay = self.cfg.get("step_delay", 300) + random.randint(0, int(self.cfg.get("delay_jitter", 0)))
                self._sleep(delay / 1000)

    def _exec(self, s, i, goto_count):
        t = s["type"]
        if t in ("tap_image", "wait_image", "wait_gone"):
            present = t != "wait_gone"
            box, score = self._search(s["template"], s["threshold"], s["timeout"], present)
            if present and box is None:
                self.log.emit(f"#{i + 1} [{s['template']}] 못 찾음 (최고 {score:.2f})")
                return self._jump(s["on_fail"], i)
            if not present and box is None:
                self.log.emit(f"#{i + 1} [{s['template']}] 사라지지 않음")
                return self._jump(s["on_fail"], i)
            if t == "tap_image":
                x, y, w, h = box
                j = int(self.cfg.get("tap_jitter", 0))
                cx = x + w / 2 + s["offset_x"] * self.sx + random.randint(-j, j)
                cy = y + h / 2 + s["offset_y"] * self.sy + random.randint(-j, j)
                self.adb.tap(cx, cy)
                self.log.emit(f"#{i + 1} [{s['template']}] 탭 ({cx:.0f},{cy:.0f}) 일치도 {score:.2f}")
            return i + 1
        if t == "if_image":
            box, score = self._search(s["template"], s["threshold"], 0)
            hit = box is not None
            self.log.emit(f"#{i + 1} [{s['template']}] {'있음' if hit else '없음'} ({score:.2f})")
            return self._jump(s["then"] if hit else s["else"], i)
        if t == "tap":
            self.adb.tap(*self._pt(s["x"], s["y"]))
        elif t == "long_press":
            x, y = self._pt(s["x"], s["y"])
            self.adb.swipe(x, y, x, y, s["ms"])
        elif t == "swipe":
            x1, y1 = self._pt(s["x1"], s["y1"])
            x2, y2 = self._pt(s["x2"], s["y2"])
            self.adb.swipe(x1, y1, x2, y2, s["ms"])
        elif t == "wait":
            self._sleep((s["ms"] + random.randint(0, max(0, s["jitter"]))) / 1000)
        elif t == "key":
            self.adb.key(s["key"])
        elif t == "text":
            self.adb.text(s["text"])
        elif t == "goto":
            times = s["times"]
            n = goto_count.get(i, 0) + 1
            if times and n > times:
                goto_count[i] = 0  # 바깥 반복에서 다시 쓸 수 있게 초기화
                return i + 1
            goto_count[i] = n
            return self._jump(s["label"], i)
        elif t == "stop":
            raise _Finish()
        return i + 1
