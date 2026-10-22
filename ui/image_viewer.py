"""Кадр предпросмотра крупно: в боковой панели вертикальное видео выходит
размером с марку, и субтитры на нём не разглядеть."""
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout)


class ImageViewerDialog(QDialog):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр")
        self.pixmap = QPixmap(image_path)
        self.scale = 1.0
        self.fit = True

        layout = QVBoxLayout(self)
        self.area = QScrollArea()
        self.area.setAlignment(Qt.AlignCenter)
        self.area.setWidgetResizable(False)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.area.setWidget(self.label)
        layout.addWidget(self.area, 1)
        # Колесо иначе прокручивало бы область, а не меняло масштаб.
        self.area.viewport().installEventFilter(self)

        bar = QHBoxLayout()
        self.info = QLabel()
        self.info.setObjectName("status_label")
        fit_btn = QPushButton("Вписать")
        fit_btn.clicked.connect(self.fit_to_window)
        one_btn = QPushButton("100%")
        one_btn.clicked.connect(lambda: self.set_scale(1.0))
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        bar.addWidget(QLabel("Колесо мыши — масштаб"))
        bar.addStretch()
        bar.addWidget(self.info)
        bar.addWidget(fit_btn)
        bar.addWidget(one_btn)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.6), int(screen.height() * 0.9))

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_to_window()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit:
            self.fit_to_window()

    def fit_to_window(self):
        if self.pixmap.isNull():
            return
        view = self.area.viewport().size()
        scale = min(view.width() / self.pixmap.width(), view.height() / self.pixmap.height())
        self.set_scale(scale, fit=True)

    def set_scale(self, scale, fit=False):
        self.fit = fit
        self.scale = max(0.05, min(8.0, scale))
        size = self.pixmap.size() * self.scale
        self.label.setPixmap(self.pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.label.resize(size)
        self.info.setText(f"{self.pixmap.width()}×{self.pixmap.height()} · {int(self.scale * 100)}%")

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.set_scale(self.scale * factor)
            return True
        return super().eventFilter(obj, event)
