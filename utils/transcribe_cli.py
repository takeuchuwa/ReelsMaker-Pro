"""Дочерний процесс распознавания речи.

Запускается отдельно от интерфейса намеренно: Qt и ctranslate2 конфликтуют при
загрузке нативных библиотек, и если Qt загрузился первым, обращение к модели
роняет процесс без единого сообщения. Отдельный процесс загруженных Qt
библиотек не наследует.

Здесь нельзя импортировать PyQt5 — ни прямо, ни через utils. Прогресс идёт в
stderr строками «PROGRESS n», слова со временем пишутся в JSON.
"""
import argparse
import json
import os
import sys

WORKER_FLAG = '--transcribe-worker'

LANGUAGE_CODES = {
    'русский': 'ru', 'russian': 'ru',
    'английский': 'en', 'english': 'en',
    'украинский': 'uk', 'ukrainian': 'uk',
}


def _emit(kind, value):
    print('%s %s' % (kind, value), file=sys.stderr, flush=True)



def _read_wav(path):
    """WAV от extract_audio (16 кГц, моно, 16 бит) как массив для модели.

    Путь к файлу faster-whisper разбирает через PyAV, а свежий PyAV убрал
    параметр, который тот передаёт, — распознавание падало на открытии файла.
    Звук уже подготовлен ffmpeg, так что декодер не нужен вовсе.
    """
    import wave
    import numpy as np
    with wave.open(path, 'rb') as handle:
        if handle.getsampwidth() != 2 or handle.getframerate() != 16000:
            raise ValueError('ожидался WAV 16 кГц, 16 бит')
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def run(args):
    from faster_whisper import WhisperModel

    _emit('STAGE', 'loading')
    # int8 на CPU: та же модель, но без тяжёлой зависимости от torch и заметно
    # быстрее на машинах без видеокарты.
    model = WhisperModel(args.model, device='cpu', compute_type='int8')

    language = None
    if args.language and args.language != 'Auto-detect':
        language = LANGUAGE_CODES.get(args.language.lower(), args.language.lower())

    _emit('STAGE', 'transcribing')
    # Подсказка с именами и терминами: модель охотнее выбирает их написание,
    # когда звук неоднозначен («Поздняков», а не «Позняков»).
    segments, info = model.transcribe(_read_wav(args.audio), language=language,
                                      word_timestamps=True,
                                      hotwords=args.hints or None)

    total = getattr(info, 'duration', 0) or 0
    words = []
    # segments — генератор: распознавание идёт по мере обхода, поэтому прогресс
    # отдаём прямо здесь. Строки собирает уже основной процесс: ему нужны
    # отдельные слова со временем для анимации и автозамен.
    for segment in segments:
        for word in getattr(segment, 'words', None) or []:
            text = word.word.strip()
            if text:
                words.append({'word': text, 'start': round(word.start, 3),
                              'end': round(word.end, 3)})
        if total > 0:
            _emit('PROGRESS', min(99, int(segment.end / total * 100)))

    with open(args.json, 'w', encoding='utf-8') as handle:
        json.dump({'words': words}, handle, ensure_ascii=False)
    _emit('PROGRESS', 100)
    return 0


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(WORKER_FLAG, action='store_true', dest='worker')
    parser.add_argument('--audio', required=True)
    parser.add_argument('--json', required=True)
    parser.add_argument('--model', default='base')
    parser.add_argument('--language', default='')
    parser.add_argument('--hints', default='')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as error:
        _emit('ERROR', error)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
