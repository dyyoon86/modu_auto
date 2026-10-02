"""스텝 종류 정의와 매크로(폴더 = macro.json + templates/*.png) 저장/불러오기."""
import json
import os

# 필드 kind: int, float, str, template, label(이동할 라벨), fail(실패 시 처리), choice:A,B,C
TEMPLATE = ("template", "이미지", "template", "")
THRESHOLD = ("threshold", "일치도 (0~1, 높을수록 엄격)", "float", 0.85)
TIMEOUT = ("timeout", "최대 대기 (초)", "float", 10.0)
ON_FAIL = ("on_fail", "끝내 못 찾으면", "fail", "stop")

STEP_TYPES = {
    "tap_image": ("이미지 찾아서 탭", [
        TEMPLATE, THRESHOLD, TIMEOUT,
        ("offset_x", "탭 위치 보정 X (px)", "int", 0),
        ("offset_y", "탭 위치 보정 Y (px)", "int", 0),
        ON_FAIL]),
    "wait_image": ("이미지 나타날 때까지 대기", [TEMPLATE, THRESHOLD, TIMEOUT, ON_FAIL]),
    "wait_gone": ("이미지 사라질 때까지 대기", [TEMPLATE, THRESHOLD, TIMEOUT, ON_FAIL]),
    "if_image": ("이미지 있으면 분기", [
        TEMPLATE, THRESHOLD,
        ("then", "있으면 이동할 라벨 (비우면 다음)", "label", ""),
        ("else", "없으면 이동할 라벨 (비우면 다음)", "label", "")]),
    "tap": ("좌표 탭", [("x", "X", "int", 0), ("y", "Y", "int", 0)]),
    "long_press": ("길게 누르기", [
        ("x", "X", "int", 0), ("y", "Y", "int", 0), ("ms", "누르는 시간 (ms)", "int", 800)]),
    "swipe": ("스와이프", [
        ("x1", "시작 X", "int", 0), ("y1", "시작 Y", "int", 0),
        ("x2", "끝 X", "int", 0), ("y2", "끝 Y", "int", 0),
        ("ms", "시간 (ms)", "int", 300)]),
    "wait": ("대기", [("ms", "대기 (ms)", "int", 1000), ("jitter", "랜덤 추가 최대 (ms)", "int", 300)]),
    "key": ("버튼 누르기", [
        ("key", "버튼", "choice:BACK,HOME,APP_SWITCH,ENTER,VOLUME_UP,VOLUME_DOWN,POWER", "BACK")]),
    "text": ("글자 입력 (영문/숫자)", [("text", "내용", "str", "")]),
    "label": ("라벨 (이동 목적지)", [("name", "라벨 이름", "str", "시작")]),
    "goto": ("라벨로 이동 (반복)", [
        ("label", "이동할 라벨", "label", ""),
        ("times", "반복 횟수 (0 = 무한)", "int", 0)]),
    "stop": ("매크로 종료", []),
}

# 스텝 추가 메뉴 그룹
STEP_GROUPS = [
    ("이미지", ["tap_image", "wait_image", "wait_gone", "if_image"]),
    ("터치", ["tap", "long_press", "swipe"]),
    ("기타", ["wait", "key", "text"]),
    ("흐름", ["label", "goto", "stop"]),
]

DEFAULT_SETTINGS = {
    "step_delay": 300,   # 스텝 사이 기본 대기 (ms)
    "delay_jitter": 200,  # 스텝 대기에 더해질 랜덤 (ms)
    "tap_jitter": 4,     # 탭 좌표 랜덤 흔들림 (px)
    "poll": 0.4,         # 이미지 재확인 간격 (초)
}


def new_step(step_type):
    step = {"type": step_type}
    for key, _, _, default in STEP_TYPES[step_type][1]:
        step[key] = default
    return step


def fail_text(v):
    return {"stop": "정지", "next": "다음"}.get(v, "→" + v)


def describe(step):
    t = step["type"]
    s = step
    title = STEP_TYPES[t][0]
    if t == "tap_image":
        return f"[{s['template']}] 찾아서 탭  (≥{s['threshold']:.2f}, {s['timeout']:g}초, 실패:{fail_text(s['on_fail'])})"
    if t == "wait_image":
        return f"[{s['template']}] 나타날 때까지  ({s['timeout']:g}초, 실패:{fail_text(s['on_fail'])})"
    if t == "wait_gone":
        return f"[{s['template']}] 사라질 때까지  ({s['timeout']:g}초, 실패:{fail_text(s['on_fail'])})"
    if t == "if_image":
        return f"[{s['template']}] 있으면 {s['then'] or '다음'} / 없으면 {s['else'] or '다음'}"
    if t == "tap":
        return f"탭 ({s['x']}, {s['y']})"
    if t == "long_press":
        return f"길게 누르기 ({s['x']}, {s['y']}) {s['ms']}ms"
    if t == "swipe":
        return f"스와이프 ({s['x1']},{s['y1']}) → ({s['x2']},{s['y2']}) {s['ms']}ms"
    if t == "wait":
        return f"대기 {s['ms']}ms (+0~{s['jitter']}ms)"
    if t == "key":
        return f"버튼 {s['key']}"
    if t == "text":
        return f"입력 \"{s['text']}\""
    if t == "label":
        return f"■ {s['name']}"
    if t == "goto":
        times = "무한" if not s["times"] else f"{s['times']}회"
        return f"↺ {s['label']} 로 이동 ({times})"
    return title


class Macro:
    def __init__(self, folder):
        self.folder = folder
        self.name = os.path.basename(folder)
        self.resolution = None  # [w, h] 매크로를 만든 기기 해상도
        self.settings = dict(DEFAULT_SETTINGS)
        self.steps = []

    @property
    def template_dir(self):
        return os.path.join(self.folder, "templates")

    @property
    def json_path(self):
        return os.path.join(self.folder, "macro.json")

    def template_path(self, name):
        return os.path.join(self.template_dir, name + ".png")

    def template_names(self):
        if not os.path.isdir(self.template_dir):
            return []
        return sorted(f[:-4] for f in os.listdir(self.template_dir) if f.lower().endswith(".png"))

    def labels(self):
        return [s["name"] for s in self.steps if s["type"] == "label"]

    @classmethod
    def create(cls, folder):
        os.makedirs(os.path.join(folder, "templates"), exist_ok=True)
        m = cls(folder)
        m.save()
        return m

    @classmethod
    def load(cls, folder):
        m = cls(folder)
        with open(m.json_path, encoding="utf-8") as f:
            data = json.load(f)
        m.resolution = data.get("resolution")
        m.settings.update(data.get("settings", {}))
        # 버전이 달라 빠진 필드는 기본값으로 채운다
        for s in data.get("steps", []):
            if s.get("type") in STEP_TYPES:
                step = new_step(s["type"])
                step.update(s)
                m.steps.append(step)
        os.makedirs(m.template_dir, exist_ok=True)
        return m

    def save(self):
        data = {"name": self.name, "version": 1, "resolution": self.resolution,
                "settings": self.settings, "steps": self.steps}
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
