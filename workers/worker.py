from PyQt5.QtCore import QThread, pyqtSignal
import os
import random
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Tuple

from utils.ffmpeg_utils import (process_single, detect_crop_dimensions, pick_random_filler,
                                plan_input_window, get_video_duration, terminate_all_ffmpeg)
from utils.constants import RANDOM_FILLER
from utils.subtitle_utils import extract_audio, generate_words_from_whisper
from utils.captions import (load_whisper_words, group_words, load_srt_lines,
                            parse_fixes, apply_fixes)


class Worker(QThread):
    progress = pyqtSignal(int, int)
    # Общий процент по всей партии: при параллельной обработке у одного
    # индикатора нет «текущего файла».
    file_progress = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    file_processing = pyqtSignal(str)
    status_update = pyqtSignal(str)
    # Субтитры всех файлов готовы и ждут проверки: {путь: строки}.
    captions_ready = pyqtSignal(object)

    def __init__(
            self,
            files: List[str],
            filters: List[str],
            zoom_mode: str,
            zoom_static: int,  # FIX: Added static zoom value
            zoom_min: int,
            zoom_max: int,
            speed_mode: str,
            speed_static: int,  # FIX: Added static speed value
            speed_min: int,
            speed_max: int,
            overlay_file: Optional[str],
            overlay_pos: str,
            out_dir: str,
            mute_audio: bool,
            output_format: str,
            blur_background: bool,
            strip_metadata: bool,
            codec: str,
            subtitle_settings: Dict,
            auto_crop: bool,
            overlay_audio: Optional[str],
            original_volume: int,
            overlay_volume: int,
            filler_path: Optional[str] = None,
            split_content_height: int = 1080,
            content_on_top: bool = True,
            uniquify: bool = False,
            uniquify_methods: dict = None,
            trims: Optional[Dict[str, Tuple[float, Optional[float]]]] = None,
            parallel: int = 1,
            review_subtitles: bool = False,
    ):
        super().__init__()
        self.files = list(files)
        self.filters = list(filters)
        self.zoom_mode = zoom_mode
        self.zoom_static = zoom_static  # FIX: Store static zoom
        self.zoom_min = zoom_min
        self.zoom_max = zoom_max
        self.speed_mode = speed_mode
        self.speed_static = speed_static  # FIX: Store static speed
        self.speed_min = speed_min
        self.speed_max = speed_max
        self.overlay_file = overlay_file
        self.overlay_pos = overlay_pos
        self.out_dir = out_dir
        self.mute_audio = mute_audio
        self.output_format = output_format
        self.blur_background = blur_background
        self.strip_metadata = strip_metadata
        self.codec = codec
        self.subtitle_settings = subtitle_settings
        self.auto_crop = auto_crop
        self.overlay_audio = overlay_audio
        self.original_volume = original_volume / 100.0
        self.overlay_volume = overlay_volume / 100.0
        self.filler_path = filler_path
        self.split_content_height = split_content_height
        self.content_on_top = content_on_top
        self.uniquify = uniquify
        self.uniquify_methods = uniquify_methods or {}
        self.trims = trims or {}
        self.parallel = max(1, int(parallel))
        self._is_running = True
        self.output_paths = []
        self.errors = []

        self._lock = threading.Lock()
        # Распознавание держит модель в памяти и само грузит все ядра: два
        # сразу только мешают друг другу.
        self._whisper_lock = threading.Lock()
        self._percent = {}
        self._active = []
        self._done = 0
        self._reserved_outputs = set()
        self.review_subtitles = review_subtitles
        self._prepared = {}
        self._failed = set()
        self._review_event = threading.Event()
        self._reviewed = None
        self.cancelled_at_review = False

    def pick_zoom(self) -> int:
        # FIX: Correctly return static or dynamic value
        if self.zoom_mode == 'dynamic' and self.zoom_max >= self.zoom_min:
            try:
                return random.randint(self.zoom_min, self.zoom_max)
            except ValueError:
                return self.zoom_min  # Fallback for dynamic mode
        return self.zoom_static  # Return the correct static value

    def pick_speed(self) -> int:
        # FIX: Correctly return static or dynamic value
        if self.speed_mode == 'dynamic' and self.speed_max >= self.speed_min:
            try:
                return random.randint(self.speed_min, self.speed_max)
            except ValueError:
                return self.speed_min  # Fallback for dynamic mode
        return self.speed_static  # Return the correct static value

    def stop(self):
        self._is_running = False
        self._review_event.set()
        terminate_all_ffmpeg()
        print("Worker stop requested.")

    def is_stopped(self):
        return not self._is_running

    def _output_path(self, in_file_path):
        base_name = os.path.basename(in_file_path)
        name_part, _ = os.path.splitext(base_name)
        suffix = "_reels" if self.output_format != "Оригинальный" else "_processed"
        with self._lock:
            # Одинаковые имена из разных папок при параллельной работе писали
            # бы в один файл одновременно.
            counter = 1
            while True:
                tail = "" if counter == 1 else f"_{counter}"
                candidate = os.path.join(self.out_dir, f"{name_part}{suffix}{tail}.mp4")
                if (os.path.abspath(candidate) != os.path.abspath(in_file_path)
                        and candidate not in self._reserved_outputs):
                    break
                counter += 1
            self._reserved_outputs.add(candidate)
        return candidate

    def _report_progress(self, index, percent):
        with self._lock:
            self._percent[index] = percent
            overall = sum(self._percent.values()) / max(1, len(self.files))
        self.file_progress.emit(int(overall))

    def _set_active(self, base_name, add):
        with self._lock:
            if add:
                self._active.append(base_name)
            elif base_name in self._active:
                self._active.remove(base_name)
            names = ", ".join(self._active)
        if names:
            self.file_processing.emit(names)

    def _prepare_captions(self, in_file_path, base_name):
        """Строки субтитров из слов со временем исходника, уже с автозаменами."""
        subtitle_mode = self.subtitle_settings.get("mode")
        trim_start, trim_end = self.trims.get(in_file_path, (0.0, None))
        caption_lines = None
        temp_audio_path = words_path = None
        try:
            if subtitle_mode == "whisper":
                temp_dir = tempfile.gettempdir()
                temp_audio_path = os.path.join(temp_dir, f"{uuid.uuid4()}.wav")
                words_path = os.path.join(temp_dir, f"{uuid.uuid4()}.json")

                start, span = plan_input_window(
                    get_video_duration(in_file_path), trim_start, trim_end, None)
                self.status_update.emit(f"Извлечение аудио из '{base_name}'...")
                extract_audio(in_file_path, temp_audio_path, start, span)

                with self._whisper_lock:
                    if not self._is_running:
                        return None
                    self.status_update.emit(f"Распознавание речи: {base_name}")
                    generate_words_from_whisper(
                        audio_path=temp_audio_path,
                        json_path=words_path,
                        model_name=self.subtitle_settings.get("model"),
                        language=self.subtitle_settings.get("language"),
                        hints=self.subtitle_settings.get("hints", ""),
                        progress_callback=lambda p: self.status_update.emit(
                            f"Распознавание речи: {base_name} — {p}%")
                    )
                # Звук вырезан с секунды start: возвращаем словам время исходника.
                caption_lines = group_words(load_whisper_words(words_path, start),
                                            self.subtitle_settings.get("words_per_line", 4))
            elif subtitle_mode == "srt_file":
                srt_path = self.subtitle_settings.get("srt_path")
                if not srt_path or not os.path.exists(srt_path):
                    raise FileNotFoundError(f"Файл субтитров не найден: {srt_path}")
                caption_lines = load_srt_lines(srt_path)

            if caption_lines:
                caption_lines = apply_fixes(caption_lines,
                                            parse_fixes(self.subtitle_settings.get("fixes", "")))
            return caption_lines
        finally:
            for path in (temp_audio_path, words_path):
                if path and os.path.exists(path):
                    os.remove(path)

    def _prepare_for_review(self, in_file_path):
        if not self._is_running:
            return
        base_name = os.path.basename(in_file_path)
        self._set_active(base_name, True)
        try:
            self._prepared[in_file_path] = self._prepare_captions(in_file_path, base_name) or []
        except Exception as e:
            if not self._is_running:
                return
            message = f"Ошибка субтитров для '{base_name}':\n{type(e).__name__}: {e}"
            with self._lock:
                self.errors.append(message)
                self._failed.add(in_file_path)
            self.error.emit(message)
        finally:
            self._set_active(base_name, False)

    def resume(self, captions):
        """Ответ окна проверки: исправленные строки или None — отмена."""
        self._reviewed = captions
        self._review_event.set()

    def _process_one(self, index, in_file_path):
        if not self._is_running:
            return
        base_name = os.path.basename(in_file_path)
        out_file_path = self._output_path(in_file_path)
        trim_start, trim_end = self.trims.get(in_file_path, (0.0, None))

        self._set_active(base_name, True)
        crop_filter = None

        try:
            if self.auto_crop:
                self.status_update.emit(f"Анализ черных полос: {base_name}")
                crop_filter = detect_crop_dimensions(in_file_path)

            if in_file_path in self._prepared:
                caption_lines = self._prepared[in_file_path]
            else:
                caption_lines = self._prepare_captions(in_file_path, base_name)
            if not self._is_running:
                return

            process_single(
                in_path=in_file_path,
                out_path=out_file_path,
                filters=self.filters,
                zoom_p=self.pick_zoom(),
                speed_p=self.pick_speed(),
                overlay_file=self.overlay_file,
                overlay_pos=self.overlay_pos,
                output_format=self.output_format,
                blur_background=self.blur_background,
                mute_audio=self.mute_audio,
                strip_metadata=self.strip_metadata,
                codec=self.codec,
                caption_lines=caption_lines,
                subtitle_style=self.subtitle_settings.get("style", {}),
                crop_filter=crop_filter,
                overlay_audio_path=self.overlay_audio,
                original_volume=self.original_volume,
                overlay_volume=self.overlay_volume,
                filler_path=(pick_random_filler()
                             if self.filler_path == RANDOM_FILLER
                             else self.filler_path),
                split_content_height=self.split_content_height,
                content_on_top=self.content_on_top,
                uniquify=self.uniquify,
                uniquify_methods=self.uniquify_methods,
                progress_callback=lambda p: self._report_progress(index, p),
                trim_start=trim_start,
                trim_end=trim_end,
            )
            with self._lock:
                self.output_paths.append(out_file_path)
                self._done += 1
                done = self._done
            self._report_progress(index, 100)
            self.progress.emit(done, len(self.files))

        except Exception as e:
            if not self._is_running:
                # ffmpeg сняли кнопкой «Остановить» — это не ошибка файла.
                if os.path.exists(out_file_path):
                    os.remove(out_file_path)
                return
            error_msg = f"Ошибка при обработке файла '{base_name}':\n{type(e).__name__}: {e}"
            if isinstance(e, subprocess.CalledProcessError) and e.output:
                error_msg += f"\n\nFFmpeg output:\n{e.output[-500:]}"
            print(f"Error in worker thread: {error_msg}")
            with self._lock:
                self.errors.append(error_msg)
            self._report_progress(index, 100)
            self.error.emit(error_msg)

        finally:
            self._set_active(base_name, False)

    def run(self):
        total_files = len(self.files)
        if total_files == 0:
            self.finished.emit()
            return

        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except OSError as e:
            message = f"Не удалось создать выходную папку: {self.out_dir}\nОшибка: {e}"
            self.errors.append(message)
            self.error.emit(message)
            self.finished.emit()
            return

        workers = min(self.parallel, total_files)
        has_subtitles = self.subtitle_settings.get("mode") in ("whisper", "srt_file")
        if self.review_subtitles and has_subtitles:
            # Сначала субтитры для всех файлов, затем пауза на проверку и
            # только потом рендер: править текст, пока видео уже кодируется,
            # было бы поздно.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for path in self.files:
                    pool.submit(self._prepare_for_review, path)
            ready = {p: self._prepared[p] for p in self.files if p in self._prepared}
            if self._is_running and ready:
                self.status_update.emit("Проверка субтитров...")
                self.captions_ready.emit(ready)
                self._review_event.wait()
                if self._reviewed is None and self._is_running:
                    self.cancelled_at_review = True
                    self._is_running = False
                elif self._reviewed is not None:
                    self._prepared.update(self._reviewed)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, path in enumerate(self.files):
                if path not in self._failed:
                    pool.submit(self._process_one, index, path)

        if self._is_running:
            print("Worker finished processing all files.")
        else:
            print("Worker finished due to stop request.")
        self.finished.emit()
