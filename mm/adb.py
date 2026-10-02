"""adb 래퍼: 기기 목록, 화면 캡처, 탭/스와이프/키 입력."""
import glob
import os
import shutil
import subprocess

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_tool(name):
    """tools/scrcpy-*/ 안의 실행파일을 우선 찾고, 없으면 PATH에서 찾는다."""
    exe = name + (".exe" if os.name == "nt" else "")
    hits = sorted(glob.glob(os.path.join(ROOT, "tools", "scrcpy-*", exe)))
    return hits[-1] if hits else shutil.which(name)


class Adb:
    def __init__(self):
        self.path = find_tool("adb")
        self.serial = None

    def _run(self, *args, timeout=15, use_serial=True):
        if not self.path:
            raise RuntimeError("adb를 찾을 수 없습니다. tools 폴더에 scrcpy를 넣어주세요.")
        cmd = [self.path]
        if use_serial and self.serial:
            cmd += ["-s", self.serial]
        r = subprocess.run(cmd + list(args), capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        if r.returncode != 0:
            msg = r.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(msg or "adb 명령 실패: " + " ".join(args))
        return r.stdout

    def devices(self):
        """[(serial, state)] — state가 'device'여야 사용 가능."""
        out = self._run("devices", use_serial=False).decode("utf-8", "replace")
        result = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                result.append((parts[0], parts[1]))
        return result

    def connect(self, addr):
        return self._run("connect", addr, use_serial=False).decode("utf-8", "replace").strip()

    def screencap(self):
        data = self._run("exec-out", "screencap", "-p", timeout=20)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("화면 캡처 실패 (기기 화면이 꺼져 있거나 보안 화면일 수 있음)")
        return img

    def tap(self, x, y):
        self._run("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, ms):
        self._run("shell", "input", "swipe", str(int(x1)), str(int(y1)),
                  str(int(x2)), str(int(y2)), str(int(ms)), timeout=15 + ms / 1000)

    def key(self, name):
        self._run("shell", "input", "keyevent", "KEYCODE_" + name)

    def text(self, s):
        # adb input text는 공백을 %s로 받는다. 한글은 지원되지 않는다.
        self._run("shell", "input", "text", s.replace(" ", "%s"))
