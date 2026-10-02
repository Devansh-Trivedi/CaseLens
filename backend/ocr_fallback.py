"""Local CPU OCR fallback. Models ship with the optional pip package.

Run in a Python 3.10–3.12 environment; no case text leaves this computer.
"""
import sys

if __name__ == '__main__':
    try:
        from rapidocr_onnxruntime import RapidOCR
        result, _ = RapidOCR()(sys.argv[1])
        print('\n'.join(item[1] for item in (result or [])))
    except Exception:
        print('Local OCR unavailable.', file=sys.stderr)
        sys.exit(1)
