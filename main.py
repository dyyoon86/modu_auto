import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from mm.gui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Malgun Gothic", 9))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
