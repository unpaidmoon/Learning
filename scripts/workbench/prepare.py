"""Prepare fixed product resources without any research runtime dependency."""

import argparse
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.workbench.resources import ReadingResources
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.ocr_resources import OcrResources
from learning_app.workbench.scheduler import CalculationHelper
from learning_app.workbench.essay_processing import EssayProcessor
from learning_app.workbench.offline_resources import OfflineRuntime
from learning_app.workbench.content_processing import ContentProcessor

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdfjs-archive", type=Path)
    parser.add_argument("--asr-wheelhouse", type=Path)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--ocr-import-from", type=Path)
    parser.add_argument("--fsrs-executable", type=Path)
    parser.add_argument("--question-executable", type=Path)
    parser.add_argument("--essay-import-from", type=Path)
    parser.add_argument("--offline-import-from", type=Path)
    parser.add_argument("--content-import-from", type=Path)
    args = parser.parse_args()
    directory = ReadingResources(PROJECT_ROOT).prepare(args.pdfjs_archive)
    print(f"PDF.js prepared: {directory}")
    if args.asr_wheelhouse or args.asr_model:
        directory = AsrResources(PROJECT_ROOT).prepare(
            wheelhouse=args.asr_wheelhouse, model=args.asr_model
        )
        print(f"ASR imports prepared: {directory}")
    if args.ocr or args.ocr_import_from:
        directory = OcrResources(PROJECT_ROOT).prepare(args.ocr_import_from)
        print(f"OCR prepared: {directory}")
    if args.fsrs_executable:
        executable = CalculationHelper("fsrs", PROJECT_ROOT).prepare(
            args.fsrs_executable
        )
        print(f"FSRS prepared: {executable}")
    if args.question_executable:
        executable = CalculationHelper("question", PROJECT_ROOT).prepare(
            args.question_executable
        )
        print(f"Question grader prepared: {executable}")
    if args.essay_import_from:
        directory = EssayProcessor(PROJECT_ROOT).prepare(args.essay_import_from)
        print(f"Essay parser prepared: {directory}")
    if args.offline_import_from:
        directory = OfflineRuntime(PROJECT_ROOT).prepare(args.offline_import_from)
        print(f"Offline reader prepared: {directory}")
    if args.content_import_from:
        directory = ContentProcessor(PROJECT_ROOT).prepare(args.content_import_from)
        print(f"Source content processor prepared: {directory}")
