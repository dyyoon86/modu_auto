"""템플릿 매칭과 한글 경로 안전한 이미지 입출력."""
import cv2
import numpy as np


def imread(path):
    # cv2.imread는 Windows 한글 경로를 못 읽는다
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


def imwrite(path, img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("이미지 저장 실패: " + path)
    buf.tofile(path)


def find(screen, tmpl, scale=1.0):
    """screen에서 tmpl을 찾아 (점수 0~1, (x, y, w, h))를 돌려준다.

    scale은 매크로를 만든 해상도 대비 현재 기기 해상도 비율.
    해상도가 다르면 기기마다 UI 배율이 미묘하게 달라서 주변 배율도 같이 시도한다.
    """
    if abs(scale - 1.0) <= 0.01:
        return _find_at(screen, tmpl, 1.0)
    best = (0.0, None)
    for f in (1.0, 0.96, 1.04, 0.92, 1.08):
        r = _find_at(screen, tmpl, scale * f)
        if r[0] > best[0]:
            best = r
        if best[0] >= 0.95:
            break
    return best


def _find_at(screen, tmpl, scale):
    if abs(scale - 1.0) > 0.01:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        tmpl = cv2.resize(tmpl, None, fx=scale, fy=scale, interpolation=interp)
    th, tw = tmpl.shape[:2]
    if th < 2 or tw < 2 or screen.shape[0] < th or screen.shape[1] < tw:
        return 0.0, None
    res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)  # 단색 템플릿 대비
    _, score, _, loc = cv2.minMaxLoc(res)
    return float(score), (loc[0], loc[1], tw, th)
