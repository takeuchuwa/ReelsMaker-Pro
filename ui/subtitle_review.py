"""Проверка субтитров между распознаванием и рендером.

Whisper ошибается в именах и терминах; автозамены ловят повторяющееся, а
разовое проще поправить глазами, пока видео ещё не закодировано.
"""
import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                             QListWidget, QMessageBox, QPushButton, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget, QApplication, QShortcut)

from utils.captions import edit_line_text, retime_line
from utils.timecode import format_timecode, parse_timecode

COL_START, COL_END, COL_TEXT = range(3)


class SubtitleReviewDialog(QDialog):
    def __init__(self, captions, play_callback=None, parent=None, player=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка субтитров")
        self.play_callback = play_callback
        self.player = player
        # Пока идёт прослушивание, выделение в таблице следует за звуком.
        self._following = False
        self._play_end = None
        # Своя копия: «Отменить» не должно оставлять полуправленные строки.
        self.captions = {path: [list(line) for line in lines] for path, lines in captions.items()}
        self.paths = list(self.captions)
        self.path = None
        self._filling = False
        self._build()
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.55), int(screen.height() * 0.75))
        self.file_list.setCurrentRow(0)

    def _build(self):
        layout = QVBoxLayout(self)

        hint = QLabel("Двойной клик — править текст или время. Символ | внутри текста делит "
                      "строку на две; пустой текст удаляет строку.")
        hint.setWordWrap(True)
        hint.setObjectName("status_label")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)
        self.file_list = QListWidget()
        for path in self.paths:
            self.file_list.addItem(os.path.basename(path))
        self.file_list.currentRowChanged.connect(self._on_file)
        # Один файл — список не нужен, он только отнимал бы место.
        self.file_list.setVisible(len(self.paths) > 1)
        splitter.addWidget(self.file_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Начало", "Конец", "Текст"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_START, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_END, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_TEXT, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        right_layout.addWidget(self.table, 1)

        tools = QHBoxLayout()
        self.play_btn = QPushButton("▶ Прослушать строку")
        self.play_btn.setToolTip("Проиграть строку в плеере главного окна")
        self.play_btn.clicked.connect(self._play)
        self.play_btn.setEnabled(self.play_callback is not None)
        self.play_all_btn = QPushButton("▶▶ Прослушать всё")
        self.play_all_btn.setToolTip("Без остановки с выделенной строки до конца")
        self.play_all_btn.clicked.connect(self._play_all)
        self.play_all_btn.setEnabled(self.play_callback is not None)
        self.pause_btn = QPushButton("⏸ Пауза")
        self.pause_btn.setToolTip("Пауза / продолжить  Ctrl+Пробел")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.pause_btn.setEnabled(False)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self._toggle_pause)
        if self.player is not None:
            self.player.position_changed.connect(self._on_position)
            self.player.playing_changed.connect(self._on_playing)
        merge_btn = QPushButton("Склеить со следующей")
        merge_btn.clicked.connect(self._merge)
        delete_btn = QPushButton("Удалить строку")
        delete_btn.clicked.connect(self._delete)
        self.count_label = QLabel()
        self.count_label.setObjectName("status_label")
        tools.addWidget(self.play_btn)
        tools.addWidget(self.play_all_btn)
        tools.addWidget(self.pause_btn)
        tools.addWidget(merge_btn)
        tools.addWidget(delete_btn)
        tools.addStretch()
        tools.addWidget(self.count_label)
        right_layout.addLayout(tools)
        splitter.addWidget(right)
        splitter.setSizes([220, 900])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        cancel_btn = QPushButton("Отменить обработку")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Продолжить")
        ok_btn.setObjectName("ok_button")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(ok_btn)
        layout.addLayout(buttons)

    # --- данные --------------------------------------------------------
    def result_captions(self):
        return {path: sorted((l for l in lines if l), key=lambda l: l[0].start)
                for path, lines in self.captions.items()}

    def _lines(self):
        return self.captions[self.path]

    def _on_file(self, row):
        if 0 <= row < len(self.paths):
            if self._following and self.player is not None:
                self.player.set_paused(True)
            self._following = False
            self.pause_btn.setEnabled(False)
            self.path = self.paths[row]
            self._fill()

    def _fill(self, keep_row=None):
        self._filling = True
        try:
            lines = self._lines()
            self.table.setRowCount(len(lines))
            for row, line in enumerate(lines):
                values = (format_timecode(line[0].start), format_timecode(line[-1].end),
                          " ".join(w.text for w in line))
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col != COL_TEXT:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, col, item)
            self.count_label.setText(f"Строк: {len(lines)}")
        finally:
            self._filling = False
        if keep_row is not None and self.table.rowCount():
            self.table.selectRow(min(keep_row, self.table.rowCount() - 1))

    def _on_item_changed(self, item):
        if self._filling:
            return
        row, col = item.row(), item.column()
        lines = self._lines()
        line = lines[row]
        if col == COL_TEXT:
            lines[row:row + 1] = edit_line_text(line, item.text())
        else:
            value = parse_timecode(item.text())
            start = value if col == COL_START else line[0].start
            end = value if col == COL_END else line[-1].end
            if value is None or end - start < 0.05:
                QMessageBox.warning(self, "Неверное время",
                                    "Время в виде 12.5, 1:23.5 или 0:01:23.5; конец позже начала.")
            else:
                lines[row] = retime_line(line, start, end)
        self._fill(keep_row=row)

    def _current_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _play(self):
        row = self._current_row()
        if row is None or not self.play_callback:
            return
        line = self._lines()[row]
        self._start_playback(line[0].start, line[-1].end)

    def _play_all(self):
        lines = [l for l in self._lines() if l]
        if not lines or not self.play_callback:
            return
        row = self._current_row() or 0
        start = lines[min(row, len(lines) - 1)][0].start
        self._start_playback(start, max(l[-1].end for l in lines))

    def _start_playback(self, start, end):
        self._following = True
        self._play_end = end
        self.pause_btn.setEnabled(True)
        self.play_callback(self.path, start, end)

    def _toggle_pause(self):
        if self.player is None or not self._following:
            return
        self.player.set_paused(self.player.is_playing())

    def _on_playing(self, playing):
        self.pause_btn.setText("⏸ Пауза" if playing else "▶ Продолжить")

    def _on_position(self, t):
        if not self._following:
            return
        if self._play_end is not None and t >= self._play_end - 0.05:
            # Доиграли до конца: «Продолжить» дальше вело бы за пределы.
            self._following = False
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("⏸ Пауза")
            return
        # Пока человек печатает в ячейке, выделение не трогаем: иначе правка
        # прерывалась бы на полуслове.
        if self.table.state() == QAbstractItemView.EditingState:
            return
        lines = self._lines()
        row = next((i for i, l in enumerate(lines) if l and l[0].start <= t < l[-1].end + 0.3), None)
        if row is not None and row != self._current_row():
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, COL_TEXT))

    def done(self, result):
        # Рендер начнётся сразу: звук из плеера в это время только мешал бы.
        if self.player is not None:
            self.player.set_paused(True)
            try:
                self.player.position_changed.disconnect(self._on_position)
                self.player.playing_changed.disconnect(self._on_playing)
            except TypeError:
                pass
        super().done(result)

    def _merge(self):
        row = self._current_row()
        lines = self._lines()
        if row is None or row + 1 >= len(lines):
            return
        lines[row:row + 2] = [lines[row] + lines[row + 1]]
        self._fill(keep_row=row)

    def _delete(self):
        row = self._current_row()
        if row is None:
            return
        del self._lines()[row]
        self._fill(keep_row=row)
