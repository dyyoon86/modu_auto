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


def find(screen, tmpl, scale=1.0, near=None, threshold=0.0):
    """screen에서 tmpl을 찾아 (점수 0~1, (x, y, w, h))를 돌려준다.

    scale: 매크로를 만든 해상도 대비 현재 기기 해상도 비율.
        해상도가 다르면 기기마다 UI 배율이 미묘하게 달라서 주변 배율도 같이 시도한다.
    near: (x, y) 현재 화면 좌표. 똑같이 생긴 게 여러 개면 이 위치에서 가장 가까운 것을 고른다.
    """
    scales = [1.0] if abs(scale - 1.0) <= 0.01 else [scale * f for f in (1.0, 0.96, 1.04, 0.92, 1.08)]
    best, best_res = (0.0, None), None
    for s in scales:
        m = _match(screen, tmpl, s)
        if m is None:
            continue
        res, tw, th = m
        _, score, _, loc = cv2.minMaxLoc(res)
        if score > best[0]:
            best, best_res = (float(score), (loc[0], loc[1], tw, th)), m
        if best[0] >= 0.95:
            break
    if near is None or best_res is None:
        return best
    res, tw, th = best_res
    ys, xs = np.where(res >= max(best[0] - 0.05, threshold))
    if len(xs) <= 1:
        return best
    i = int(np.argmin((xs + tw / 2 - near[0]) ** 2 + (ys + th / 2 - near[1]) ** 2))
    return float(res[ys[i], xs[i]]), (int(xs[i]), int(ys[i]), tw, th)


def _nan0(a):
    return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)


def quality(screen, box):
    """화면에서 box 영역을 템플릿으로 쓸 때 얼마나 믿을 만한지 평가.

    반환: (level, 메시지, 화면 내 다른 곳의 최고 일치도) — level은 good / warn / bad
    """
    x, y, w, h = box
    sh, sw = screen.shape[:2]
    if w < 12 or h < 12:
        return "bad", "너무 작아요 — 조금 더 크게 잘라주세요", 1.0
    if w * h > sw * sh * 0.2:
        return "bad", "너무 커요 — 배경이 바뀌면 못 찾아요. 버튼만 작게 잘라주세요", 1.0
    tmpl = screen[y:y + h, x:x + w]
    if cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY).std() < 6:
        return "bad", "무늬가 거의 없어요 — 글자나 아이콘이 들어가게 잘라주세요", 1.0
    # 화면의 다른 곳에 비슷한 게 또 있는지 (속도 위해 절반 크기로 검사)
    f = 0.5 if sw > 800 else 1.0
    s = cv2.resize(screen, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f != 1 else screen
    t = cv2.resize(tmpl, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f != 1 else tmpl
    th, tw = t.shape[:2]
    res = _nan0(cv2.matchTemplate(s, t, cv2.TM_CCOEFF_NORMED))
    px, py = int(x * f), int(y * f)
    res[max(0, py - th // 2):py + th // 2 + 1, max(0, px - tw // 2):px + tw // 2 + 1] = -1  # 자기 자신 제외
    second = float(res.max()) if res.size else 0.0
    if second >= LOOKALIKE:
        return "warn", f"비슷하게 생긴 게 화면에 또 있어요 (일치도 {second:.2f})", second
    if w < 20 or h < 20:
        return "warn", "조금 작아요 — 해상도가 다른 기기에서 놓칠 수 있어요", second
    return "good", "좋아요", second


LOOKALIKE = 0.85     # 기본 일치도 기준. 다른 곳이 이 이상이면 헷갈릴 수 있음
MAX_THRESHOLD = 0.93  # 해상도 보정 시 진짜 버튼 점수가 0.95 안팎이라 이보다 높이면 못 찾는다

_RANK = {"bad": 0, "warn": 1, "good": 2}


def suggest_threshold(second):
    """닮은꼴(second)에는 반응하지 않도록 일치도 기준을 올린다."""
    return round(min(MAX_THRESHOLD, max(LOOKALIKE, second + 0.03)), 2)


def _element_boxes(screen, x, y, base):
    """누른 점을 감싸는 UI 요소(버튼 등)의 테두리를 윤곽선으로 찾는다. 작은 것부터."""
    sh, sw = screen.shape[:2]
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_side = max(24, int(base * 0.4))
    found = set()
    for c in contours:
        bx, by, bw, bh = cv2.boundingRect(c)
        if not (bx <= x < bx + bw and by <= y < by + bh):
            continue
        if bw < min_side or bh < min_side or bw * bh > sw * sh * 0.15:
            continue
        pad = 4
        x0, y0 = max(0, bx - pad), max(0, by - pad)
        found.add((x0, y0, min(sw, bx + bw + pad) - x0, min(sh, by + bh + pad) - y0))
    return sorted(found, key=lambda b: b[2] * b[3])[:4]


def auto_crop(screen, x, y):
    """(x, y)를 누른 곳의 템플릿 후보(버튼 테두리, 여러 크기 정사각형)를 평가해 가장 좋은 것을 고른다.

    반환: ((x, y, w, h), level, 메시지, 닮은꼴 최고 일치도)
    """
    sh, sw = screen.shape[:2]
    base = max(48, int(min(sw, sh) * 0.09))
    candidates = _element_boxes(screen, x, y, base)
    for k in (1.0, 1.5, 0.7, 2.0, 2.8):
        size = min(int(base * k), sw, sh)
        candidates.append((max(0, min(sw - size, x - size // 2)), max(0, min(sh - size, y - size // 2)), size, size))
    best = None
    for box in candidates:  # 버튼 테두리 → 기본 정사각형 순. 좋음이 나오면 바로 멈춤
        level, msg, second = quality(screen, box)
        if best is None or (_RANK[level], -second) > (_RANK[best[1]], -best[3]):
            best = (box, level, msg, second)
        if level == "good":
            break
    return best


def _match(screen, tmpl, scale):
    """템플릿을 scale배 해서 매칭한 점수 지도와 템플릿 크기. 화면보다 크면 None."""
    if abs(scale - 1.0) > 0.01:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        tmpl = cv2.resize(tmpl, None, fx=scale, fy=scale, interpolation=interp)
    th, tw = tmpl.shape[:2]
    if th < 2 or tw < 2 or screen.shape[0] < th or screen.shape[1] < tw:
        return None
    res = _nan0(cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED))  # 단색 템플릿 대비
    return res, tw, th
