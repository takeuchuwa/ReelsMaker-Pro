"""Плеер и таймлайн для выбора отрезка.

Видео показывает libmpv через OpenGL: встраивание по идентификатору окна на
Wayland не работает, а встроенный плеер Qt5 в Linux зависит от GStreamer и
неточно перематывает. Таймлайн рисуется вручную: миниатюры кадров, ручки
начала и конца отрезка, указатель позиции, масштаб колесом мыши.
"""
import locale
import logging
import subprocess
from typing import Optional

from PyQt5.QtCore import Qt, QByteArray, QRectF, QPointF, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import (QColor, QImage, QPainter, QPen, QPolygonF, QFontMetrics,
                         QKeySequence, QOpenGLContext)
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QOpenGLWidget, QPushButton,
                             QShortcut, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)
import qtawesome as qta

from utils.ffmpeg_utils import FFMPEG_PATH_EFFECTIVE, get_video_duration
from utils.timecode import format_timecode, parse_timecode

logger = logging.getLogger(__name__)

try:
    import mpv
    MPV_ERROR = None
except Exception as error:  # нет libmpv или самого модуля
    mpv = None
    MPV_ERROR = str(error)

ACCENT = QColor('#4ecdc4')
PLAYHEAD = QColor('#ff6b6b')
MIN_SPAN = 0.1


class MpvView(QOpenGLWidget):
    """Кадр mpv внутри окна Qt."""
    position_changed = pyqtSignal(float)
    duration_changed = pyqtSignal(float)
    pause_changed = pyqtSignal(bool)
    _redraw = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)
        # mpv требует точку как десятичный разделитель, а Qt при старте
        # выставляет системную локаль — в русской это запятая.
        locale.setlocale(locale.LC_NUMERIC, 'C')
        self.player = mpv.MPV(
            vo='libmpv', keep_open='always', pause=True, idle=True,
            osc=False, input_default_bindings=False, input_vo_keyboard=False,
            hwdec='auto-safe', ytdl=False, loglevel='warn',
            log_handler=lambda level, prefix, text: logger.info(f"mpv[{prefix}] {text.strip()}"))
        self.ctx = None
        self._proc_address_fn = None
        self._redraw.connect(self.update)
        # Обработчики свойств вызываются из потока mpv: только сигналы.
        self.player.observe_property('time-pos', lambda _n, v: v is not None and self.position_changed.emit(float(v)))
        self.player.observe_property('duration', lambda _n, v: v is not None and self.duration_changed.emit(float(v)))
        self.player.observe_property('pause', lambda _n, v: self.pause_changed.emit(bool(v)))

    def _get_proc_address(self, _ctx, name):
        context = QOpenGLContext.currentContext()
        if context is None:
            return 0
        address = context.getProcAddress(QByteArray(name))
        return int(address) if address else 0

    def initializeGL(self):
        self._proc_address_fn = mpv.MpvGlGetProcAddressFn(self._get_proc_address)
        self.ctx = mpv.MpvRenderContext(
            self.player, 'opengl',
            opengl_init_params={'get_proc_address': self._proc_address_fn})
        self.ctx.update_cb = self._redraw.emit

    def paintGL(self):
        if self.ctx is None:
            return
        ratio = self.devicePixelRatioF()
        self.ctx.render(flip_y=True, opengl_fbo={
            'w': int(self.width() * ratio), 'h': int(self.height() * ratio),
            'fbo': self.defaultFramebufferObject()})

    def shutdown(self):
        try:
            if self.ctx is not None:
                self.makeCurrent()
                self.ctx.free()
                self.ctx = None
            self.player.terminate()
        except Exception as error:
            logger.warning(f"mpv shutdown: {error}")


class ThumbnailLoader(QThread):
    """Кадры для полосы таймлайна: по одному ffmpeg на кадр с быстрым поиском.

    Прогон всего файла через фильтр fps на длинном видео шёл бы десятки
    секунд; отдельный поиск по ключевым кадрам — доли секунды на миниатюру.
    """
    thumbnail = pyqtSignal(int, QImage)

    def __init__(self, path, duration, count, parent=None):
        super().__init__(parent)
        self.path, self.duration, self.count = path, duration, count
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        if not FFMPEG_PATH_EFFECTIVE or self.duration <= 0:
            return
        for index in range(self.count):
            if self._stopped:
                return
            at = (index + 0.5) * self.duration / self.count
            cmd = [FFMPEG_PATH_EFFECTIVE, '-v', 'error', '-ss', f'{at:.3f}', '-i', self.path,
                   '-frames:v', '1', '-vf', 'scale=-2:96', '-f', 'image2pipe', '-c:v', 'mjpeg',
                   '-q:v', '5', '-']
            try:
                data = subprocess.run(cmd, capture_output=True, timeout=15,
                                      stdin=subprocess.DEVNULL).stdout
            except Exception:
                continue
            image = QImage.fromData(data, 'JPG')
            if not image.isNull() and not self._stopped:
                self.thumbnail.emit(index, image)


class Timeline(QWidget):
    """Шкала времени с миниатюрами, ручками отрезка и указателем позиции."""
    seek_requested = pyqtSignal(float, bool)   # время, точный поиск
    range_changed = pyqtSignal(float, object)  # начало, конец или None
    RULER = 20
    STRIP = 60
    HANDLE = 9
    THUMBS = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(self.RULER + self.STRIP + 4)
        self.setMaximumHeight(self.RULER + self.STRIP + 4)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.duration = 0.0
        self.position = 0.0
        self.in_point = 0.0
        self.out_point = None
        self.view = (0.0, 0.0)
        self.thumbs = {}
        self._drag = None

    # --- состояние -----------------------------------------------------
    def reset(self, duration):
        self.duration = max(0.0, duration)
        self.view = (0.0, self.duration)
        self.position = 0.0
        self.thumbs = {}
        self.update()

    def set_range(self, start, end):
        self.in_point = start or 0.0
        self.out_point = end
        self.update()

    def set_position(self, seconds):
        if self._drag == 'seek':
            return
        self.position = seconds
        v0, v1 = self.view
        # Во время воспроизведения указатель не должен уезжать за край.
        if self.duration and (seconds > v1 or seconds < v0) and v1 - v0 < self.duration:
            span = v1 - v0
            start = min(max(0.0, seconds - span * 0.1), self.duration - span)
            self.view = (start, start + span)
        self.update()

    def set_thumbnail(self, index, image):
        self.thumbs[index] = image
        self.update()

    def effective_out(self):
        return self.out_point if self.out_point is not None else self.duration

    # --- геометрия -----------------------------------------------------
    def _t_to_x(self, t):
        v0, v1 = self.view
        if v1 <= v0:
            return 0.0
        return (t - v0) / (v1 - v0) * self.width()

    def _x_to_t(self, x):
        v0, v1 = self.view
        if self.width() <= 0:
            return 0.0
        return min(max(v0 + x / self.width() * (v1 - v0), 0.0), self.duration)

    # --- отрисовка -----------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        pal = self.palette()
        p.fillRect(self.rect(), QColor('#1b1d21'))
        if self.duration <= 0:
            p.setPen(QColor('#868c96'))
            p.drawText(self.rect(), Qt.AlignCenter, "Нет видео")
            return

        self._paint_ruler(p, w)
        strip = QRectF(0, self.RULER, w, self.STRIP)
        self._paint_thumbs(p, strip)

        x_in = self._t_to_x(self.in_point)
        x_out = self._t_to_x(self.effective_out())
        shade = QColor(0, 0, 0, 165)
        if x_in > 0:
            p.fillRect(QRectF(0, strip.top(), min(x_in, w), strip.height()), shade)
        if x_out < w:
            p.fillRect(QRectF(max(0, x_out), strip.top(), w - max(0, x_out), strip.height()), shade)

        pen = QPen(ACCENT, 2)
        p.setPen(pen)
        p.drawLine(QPointF(x_in, strip.top() + 1), QPointF(x_out, strip.top() + 1))
        p.drawLine(QPointF(x_in, strip.bottom() - 1), QPointF(x_out, strip.bottom() - 1))
        for x, left in ((x_in, True), (x_out, False)):
            rect = QRectF(x - self.HANDLE if left else x, strip.top(), self.HANDLE, strip.height())
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QPen(QColor('#12302c'), 1.5))
            cx = rect.center().x()
            p.drawLine(QPointF(cx, strip.top() + 20), QPointF(cx, strip.bottom() - 20))

        x_pos = self._t_to_x(self.position)
        p.setPen(QPen(PLAYHEAD, 2))
        p.drawLine(QPointF(x_pos, 4), QPointF(x_pos, strip.bottom()))
        p.setPen(Qt.NoPen)
        p.setBrush(PLAYHEAD)
        p.drawPolygon(QPolygonF([QPointF(x_pos - 6, 0), QPointF(x_pos + 6, 0), QPointF(x_pos, 8)]))

    def _paint_ruler(self, p, w):
        v0, v1 = self.view
        span = max(v1 - v0, 1e-6)
        steps = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
        step = next((s for s in steps if s / span * w >= 80), steps[-1])
        p.setPen(QColor('#868c96'))
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
        p.setFont(font)
        t = (int(v0 / step)) * step
        while t <= v1 + 1e-9:
            x = self._t_to_x(t)
            p.drawLine(QPointF(x, self.RULER - 6), QPointF(x, self.RULER))
            label = format_timecode(t, with_ms=step < 1)
            if step < 1:
                label = label[:-2]  # десятые доли достаточно
            p.drawText(QPointF(x + 3, self.RULER - 8), label)
            for k in range(1, 5):
                xm = self._t_to_x(t + step * k / 5)
                p.drawLine(QPointF(xm, self.RULER - 3), QPointF(xm, self.RULER))
            t += step

    def _paint_thumbs(self, p, strip):
        p.fillRect(strip, QColor('#24272e'))
        if not self.thumbs:
            return
        p.save()
        p.setClipRect(strip)
        slot = self.duration / self.THUMBS
        for index, image in self.thumbs.items():
            x0 = self._t_to_x(index * slot)
            x1 = self._t_to_x((index + 1) * slot)
            if x1 < 0 or x0 > self.width():
                continue
            tw = image.width() * strip.height() / max(1, image.height())
            # При увеличении масштаба кадр повторяется, а не растягивается.
            x = x0
            p.save()
            p.setClipRect(QRectF(x0, strip.top(), x1 - x0, strip.height()))
            while x < x1:
                p.drawImage(QRectF(x, strip.top(), tw, strip.height()), image)
                x += tw
            p.restore()
        p.restore()

    # --- мышь ----------------------------------------------------------
    def _hit(self, x):
        x_in = self._t_to_x(self.in_point) - self.HANDLE / 2
        x_out = self._t_to_x(self.effective_out()) + self.HANDLE / 2
        if abs(x - x_in) <= self.HANDLE:
            return 'in'
        if abs(x - x_out) <= self.HANDLE:
            return 'out'
        return None

    def mousePressEvent(self, event):
        if self.duration <= 0 or event.button() != Qt.LeftButton:
            return
        self._drag = self._hit(event.x()) or 'seek'
        self._drag_to(event.x(), exact=False)

    def mouseMoveEvent(self, event):
        if self._drag:
            self._drag_to(event.x(), exact=False)
        elif self.duration > 0:
            self.setCursor(Qt.SizeHorCursor if self._hit(event.x()) else Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if self._drag:
            self._drag_to(event.x(), exact=True)
        self._drag = None

    def _drag_to(self, x, exact):
        t = self._x_to_t(x)
        if self._drag == 'in':
            t = min(t, self.effective_out() - MIN_SPAN)
            self.in_point = max(0.0, t)
            self.range_changed.emit(self.in_point, self.out_point)
        elif self._drag == 'out':
            t = max(t, self.in_point + MIN_SPAN)
            # Конец у самого края — это «до конца видео», а не тайм-код.
            self.out_point = None if t >= self.duration - 0.001 else t
            self.range_changed.emit(self.in_point, self.out_point)
        self.position = t
        self.seek_requested.emit(t, exact)
        self.update()

    def wheelEvent(self, event):
        if self.duration <= 0:
            return
        v0, v1 = self.view
        span = v1 - v0
        delta = event.angleDelta().y() or event.angleDelta().x()
        if event.modifiers() & Qt.ShiftModifier or event.angleDelta().x():
            # Сдвиг по времени без изменения масштаба.
            shift = -delta / 120 * span * 0.15
            start = min(max(0.0, v0 + shift), self.duration - span)
            self.view = (start, start + span)
        else:
            anchor = self._x_to_t(event.x())
            factor = 0.8 if delta > 0 else 1.25
            new_span = min(self.duration, max(0.5, span * factor))
            ratio = (anchor - v0) / span if span else 0.5
            start = min(max(0.0, anchor - ratio * new_span), self.duration - new_span)
            self.view = (start, start + new_span)
        self.update()

    def mouseDoubleClickEvent(self, _event):
        self.view = (0.0, self.duration)
        self.update()


class PlayerPanel(QWidget):
    """Плеер, таймлайн и поля тайм-кодов для текущего видео в списке."""
    trim_changed = pyqtSignal(str, float, object)   # путь, начало, конец/None
    apply_to_all = pyqtSignal(float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = None
        self.duration = 0.0
        self.start = 0.0
        self.end = None
        self.thumb_loader = None
        self.view = None
        self._build()
        self._set_enabled(False)

    # --- интерфейс -----------------------------------------------------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.stack = QStackedWidget()
        self.placeholder = QLabel("Выберите видео в списке")
        self.placeholder.setObjectName("previewLabel")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setMinimumHeight(200)
        self.placeholder.setWordWrap(True)
        self.stack.addWidget(self.placeholder)
        if mpv is not None:
            try:
                self.view = MpvView()
                self.stack.addWidget(self.view)
                self.view.position_changed.connect(self._on_position)
                self.view.duration_changed.connect(self._on_duration)
                self.view.pause_changed.connect(self._on_pause)
            except Exception as error:
                logger.error(f"mpv init failed: {error}")
                self.view = None
                self.placeholder.setText(f"Плеер недоступен: {error}\nТайм-коды можно ввести вручную.")
        else:
            self.placeholder.setText(f"Плеер недоступен (нужен пакет mpv): {MPV_ERROR}\n"
                                     "Тайм-коды можно ввести вручную.")
        layout.addWidget(self.stack, 1)

        self.timeline = Timeline()
        self.timeline.seek_requested.connect(self._seek)
        self.timeline.range_changed.connect(self._on_timeline_range)
        self.timeline.setToolTip(
            "Клик — перейти к моменту. Ручки по краям — начало и конец отрезка.\n"
            "Колесо — масштаб, Shift+колесо — прокрутка, двойной клик — весь ролик.")
        layout.addWidget(self.timeline)

        def tool(icon, tip, slot):
            btn = QPushButton()
            btn.setIcon(qta.icon(icon, color='#c8ccd2'))
            btn.setToolTip(tip)
            btn.setFixedWidth(40)
            btn.clicked.connect(slot)
            return btn

        transport = QHBoxLayout()
        transport.setSpacing(6)
        self.btn_to_in = tool('fa5s.fast-backward', "К началу отрезка  [", self.go_to_in)
        self.btn_back = tool('fa5s.step-backward', "Кадр назад  ←\nShift+← — секунда назад", self.frame_back)
        self.btn_play = tool('fa5s.play', "Воспроизвести / пауза  Пробел", self.toggle_play)
        self.btn_fwd = tool('fa5s.step-forward', "Кадр вперёд  →\nShift+→ — секунда вперёд", self.frame_forward)
        self.btn_to_out = tool('fa5s.fast-forward', "К концу отрезка  ]", self.go_to_out)
        self.btn_loop = tool('fa5s.redo', "Повторять только отрезок", self._on_loop)
        self.btn_loop.setCheckable(True)
        self.btn_mute = tool('fa5s.volume-up', "Звук", self._on_mute)
        self.btn_mute.setCheckable(True)
        self.time_label = QLabel("00:00.000 / 00:00.000")
        for w in (self.btn_to_in, self.btn_back, self.btn_play, self.btn_fwd, self.btn_to_out):
            transport.addWidget(w)
        transport.addSpacing(8)
        transport.addWidget(self.time_label)
        transport.addStretch()
        self.span_label = QLabel("")
        self.span_label.setObjectName("status_label")
        transport.addWidget(self.span_label)
        transport.addSpacing(8)
        transport.addWidget(self.btn_loop)
        transport.addWidget(self.btn_mute)
        layout.addLayout(transport)

        trim = QHBoxLayout()
        trim.setSpacing(6)
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        for edit, tip in ((self.start_edit, "Начало: 83.5, 1:23.5 или 0:01:23.5"),
                          (self.end_edit, "Конец. Пусто — до конца видео")):
            edit.setFixedWidth(110)
            edit.setToolTip(tip)
            edit.editingFinished.connect(self._on_edits)
        self.end_edit.setPlaceholderText("до конца")
        self.btn_mark_in = tool('fa5s.map-marker-alt', "Начать отрезок с текущей позиции  I", self.mark_in)
        self.btn_mark_in.setText("Отсюда")
        self.btn_mark_out = tool('fa5s.map-marker-alt', "Закончить отрезок на текущей позиции  O", self.mark_out)
        self.btn_mark_out.setText("Досюда")
        # Один значок не объяснял, что кнопка делает: подпись важнее места.
        for btn in (self.btn_mark_in, self.btn_mark_out):
            btn.setMinimumWidth(0)
            btn.setMaximumWidth(16777215)
        self.btn_reset = QPushButton("Весь ролик")
        self.btn_reset.setToolTip("Сбросить отрезок")
        self.btn_reset.clicked.connect(self.reset_trim)
        self.btn_all = QPushButton("Ко всем")
        self.btn_all.setToolTip("Применить эти тайм-коды ко всем видео в списке")
        self.btn_all.clicked.connect(lambda: self.apply_to_all.emit(self.start, self.end))
        trim.addWidget(QLabel("Начало"))
        trim.addWidget(self.start_edit)
        trim.addWidget(self.btn_mark_in)
        trim.addSpacing(10)
        trim.addWidget(QLabel("Конец"))
        trim.addWidget(self.end_edit)
        trim.addWidget(self.btn_mark_out)
        trim.addStretch(1)
        trim.addWidget(self.btn_reset)
        trim.addWidget(self.btn_all)
        layout.addLayout(trim)

        # Горячие клавиши на всё окно: человек работает со списком и
        # таймлайном, а фокус ввода при этом где угодно. Поля ввода
        # перехватывают свои клавиши сами, печатать они не мешают.
        for key, slot in (("Space", self.toggle_play), ("I", self.mark_in), ("O", self.mark_out),
                          ("Left", self.frame_back), ("Right", self.frame_forward),
                          ("Shift+Left", lambda: self._nudge(-1.0)),
                          ("Shift+Right", lambda: self._nudge(1.0)),
                          ("[", self.go_to_in), ("]", self.go_to_out)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(slot)

    def _set_enabled(self, enabled):
        for w in (self.timeline, self.btn_to_in, self.btn_back, self.btn_play, self.btn_fwd,
                  self.btn_to_out, self.btn_loop, self.btn_mute, self.start_edit, self.end_edit,
                  self.btn_mark_in, self.btn_mark_out, self.btn_reset, self.btn_all):
            w.setEnabled(enabled)
        has_player = enabled and self.view is not None
        for w in (self.btn_to_in, self.btn_back, self.btn_play, self.btn_fwd, self.btn_to_out,
                  self.btn_loop, self.btn_mute, self.btn_mark_in, self.btn_mark_out):
            w.setEnabled(has_player)

    # --- загрузка ------------------------------------------------------
    def load(self, path, start=0.0, end=None):
        if path == self.path:
            return
        self.path = path
        self.start, self.end = start or 0.0, end
        self.duration = get_video_duration(path)
        self.timeline.reset(self.duration)
        self.timeline.set_range(self.start, self.end)
        self._sync_edits()
        self._set_enabled(True)
        # Отрезок у GIF не применяется: он зацикливается целиком.
        if path.lower().endswith('.gif'):
            for w in (self.start_edit, self.end_edit, self.btn_mark_in, self.btn_mark_out,
                      self.btn_reset, self.btn_all):
                w.setEnabled(False)
        if self.view is not None:
            self.stack.setCurrentWidget(self.view)
            self.view.player.loadfile(path, start=f"{self.start:.3f}")
            self.view.player.pause = True
            self._apply_loop()
        self._start_thumbnails()

    def unload(self):
        self.path = None
        self.duration = 0.0
        self.timeline.reset(0)
        self.start_edit.clear()
        self.end_edit.clear()
        self.span_label.clear()
        self.time_label.setText("00:00.000 / 00:00.000")
        if self.view is not None:
            self.view.player.command('stop')
        self.stack.setCurrentWidget(self.placeholder)
        self._stop_thumbnails()
        self._set_enabled(False)

    def _start_thumbnails(self):
        self._stop_thumbnails()
        self.thumb_loader = ThumbnailLoader(self.path, self.duration, Timeline.THUMBS, self)
        self.thumb_loader.thumbnail.connect(self.timeline.set_thumbnail)
        self.thumb_loader.start()

    def _stop_thumbnails(self):
        if self.thumb_loader is not None:
            self.thumb_loader.stop()
            try:
                self.thumb_loader.thumbnail.disconnect()
            except TypeError:
                pass
            self.thumb_loader = None

    def current_time(self) -> Optional[float]:
        if self.path is None:
            return None
        return self.timeline.position

    def shutdown(self):
        self._stop_thumbnails()
        if self.view is not None:
            self.view.shutdown()

    # --- воспроизведение -----------------------------------------------
    def _seek(self, t, exact=True):
        if self.view is None or self.path is None:
            self.timeline.position = t
            self._update_time_label(t)
            return
        try:
            self.view.player.command_async('seek', f"{t:.3f}", 'absolute+exact' if exact else 'absolute')
        except Exception as error:
            logger.warning(f"seek failed: {error}")

    def toggle_play(self):
        if self.view is None or self.path is None:
            return
        player = self.view.player
        if player.pause and self.btn_loop.isChecked():
            pos = self.timeline.position
            if pos < self.start or pos >= self.timeline.effective_out() - 0.05:
                self._seek(self.start)
        player.pause = not player.pause

    def frame_back(self):
        if self.view is not None and self.path:
            self.view.player.command_async('frame-back-step')

    def frame_forward(self):
        if self.view is not None and self.path:
            self.view.player.command_async('frame-step')

    def _nudge(self, seconds):
        if self.path:
            self._seek(min(max(0.0, self.timeline.position + seconds), self.duration))

    def go_to_in(self):
        if self.path:
            self._seek(self.start)

    def go_to_out(self):
        if self.path:
            # Сам конец — уже за отрезком; встаём на последний кадр внутри.
            self._seek(max(self.start, self.timeline.effective_out() - 0.04))

    def _on_loop(self):
        self._apply_loop()

    def _apply_loop(self):
        if self.view is None:
            return
        player = self.view.player
        if self.btn_loop.isChecked():
            player['ab-loop-a'] = self.start
            player['ab-loop-b'] = self.end if self.end is not None else 'no'
            player['loop-file'] = 'inf' if self.end is None else 'no'
        else:
            player['ab-loop-a'] = 'no'
            player['ab-loop-b'] = 'no'
            player['loop-file'] = 'no'

    def _on_mute(self):
        if self.view is None:
            return
        muted = self.btn_mute.isChecked()
        self.view.player.mute = muted
        self.btn_mute.setIcon(qta.icon('fa5s.volume-mute' if muted else 'fa5s.volume-up', color='#c8ccd2'))

    def _on_position(self, t):
        if self.path is None:
            return
        self.timeline.set_position(t)
        self._update_time_label(t)

    def _on_duration(self, d):
        # mpv знает длительность точнее ffprobe для части контейнеров.
        if self.path and d > 0 and abs(d - self.duration) > 0.05:
            self.duration = d
            self.timeline.duration = d
            self.timeline.view = (0.0, d)
            self.timeline.update()
            self._update_time_label(self.timeline.position)

    def _on_pause(self, paused):
        self.btn_play.setIcon(qta.icon('fa5s.play' if paused else 'fa5s.pause', color='#c8ccd2'))

    def _update_time_label(self, t):
        self.time_label.setText(f"{format_timecode(t)} / {format_timecode(self.duration)}")

    # --- отрезок -------------------------------------------------------
    def mark_in(self):
        if self.path and not self.start_edit.hasFocus():
            self._set_trim(self.timeline.position, self.end)

    def mark_out(self):
        if self.path and not self.end_edit.hasFocus():
            self._set_trim(self.start, self.timeline.position)

    def reset_trim(self):
        self._set_trim(0.0, None)

    def set_trim(self, start, end):
        """Отрезок пришёл извне, например «ко всем»: без повторного сигнала."""
        self.start, self.end = start, end
        self.timeline.set_range(start, end)
        self._sync_edits()
        self._apply_loop()

    def _set_trim(self, start, end):
        start = max(0.0, start or 0.0)
        if self.duration:
            start = min(start, max(0.0, self.duration - MIN_SPAN))
        if end is not None:
            if self.duration and end >= self.duration - 0.001:
                end = None
            elif end <= start + MIN_SPAN - 1e-9:
                # Конец раньше начала: двигаем другую границу, а не отказываем.
                if end < start:
                    start, end = end, start
                if end - start < MIN_SPAN:
                    end = None
        self.start, self.end = start, end
        self.timeline.set_range(start, end)
        self._sync_edits()
        self._apply_loop()
        if self.path:
            self.trim_changed.emit(self.path, self.start, self.end)

    def _on_timeline_range(self, start, end):
        self.start, self.end = start, end
        self._sync_edits()
        self._apply_loop()
        if self.path:
            self.trim_changed.emit(self.path, self.start, self.end)

    def _on_edits(self):
        start = parse_timecode(self.start_edit.text()) if self.start_edit.text().strip() else 0.0
        end_text = self.end_edit.text().strip()
        end = parse_timecode(end_text) if end_text else None
        if start is None or (end_text and end is None):
            self._flash_error()
            self._sync_edits()
            return
        if (start, end) != (self.start, self.end):
            self._set_trim(start, end)
            self._seek(self.start)

    def _flash_error(self):
        for edit in (self.start_edit, self.end_edit):
            edit.setStyleSheet("border-color: #ff6b6b;")
        QTimer.singleShot(1200, lambda: [e.setStyleSheet("") for e in (self.start_edit, self.end_edit)])

    def _sync_edits(self):
        self.start_edit.setText(format_timecode(self.start))
        self.end_edit.setText(format_timecode(self.end) if self.end is not None else "")
        if self.duration:
            span = (self.end if self.end is not None else self.duration) - self.start
            self.span_label.setText(f"Длительность: {format_timecode(span)}")
        else:
            self.span_label.clear()
