"""스텝 편집 대화상자. model.STEP_TYPES의 필드 정의로 폼을 자동 생성한다."""
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout)

from .model import STEP_TYPES


class StepDialog(QDialog):
    def __init__(self, step, templates, labels, parent=None):
        super().__init__(parent)
        self.step = dict(step)
        title, fields = STEP_TYPES[step["type"]]
        self.setWindowTitle("스텝 편집 - " + title)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        self.getters = {}
        if not fields:
            form.addRow(QLabel("설정할 항목이 없습니다."))
        for key, label, kind, _ in fields:
            value = self.step.get(key)
            w, get = self._widget(kind, value, templates, labels)
            form.addRow(label, w)
            self.getters[key] = get
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    @staticmethod
    def _combo(items, value, editable=False):
        """items: [(표시, 값)]"""
        c = QComboBox()
        c.setEditable(editable)
        for text, data in items:
            c.addItem(text, data)
        idx = c.findData(value)
        if idx >= 0:
            c.setCurrentIndex(idx)
        elif editable:
            c.setEditText(str(value))

        def get():
            if editable and c.currentText() != c.itemText(c.currentIndex()):
                return c.currentText().strip()
            return c.currentData()
        return c, get

    def _widget(self, kind, value, templates, labels):
        if kind == "int":
            w = QSpinBox()
            w.setRange(-100000, 100000)
            w.setValue(int(value))
            return w, w.value
        if kind == "float":
            w = QDoubleSpinBox()
            w.setRange(0, 100000)
            w.setDecimals(2)
            w.setSingleStep(0.05 if float(value) <= 1 else 1)
            w.setValue(float(value))
            return w, w.value
        if kind == "str":
            w = QLineEdit(str(value))
            return w, lambda: w.text()
        if kind == "template":
            return self._combo([(t, t) for t in templates], value)
        if kind == "label":
            return self._combo([("(다음 스텝)", "")] + [(l, l) for l in labels], value, editable=True)
        if kind == "fail":
            items = [("완전 정지", "stop"), ("이번 회차 끝 (다음 반복으로)", "end"),
                     ("무시하고 다음 스텝", "next")] + [("라벨 → " + l, l) for l in labels]
            return self._combo(items, value)
        if kind.startswith("choice:"):
            return self._combo([(c, c) for c in kind[7:].split(",")], value)
        raise ValueError(kind)

    def result_step(self):
        for key, get in self.getters.items():
            self.step[key] = get()
        return self.step
