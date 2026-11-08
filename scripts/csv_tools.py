"""Lossless CSV encoding repair for Excel. No model or network calls."""
import argparse
import codecs
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import zipfile


def decode(data, encoding=None):
    if encoding:
        return data.decode(encoding, errors='strict'), encoding
    if data.startswith(codecs.BOM_UTF8):
        return data.decode('utf-8-sig'), 'utf-8-sig'
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode('utf-16'), 'utf-16'
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        return data.decode('gb18030', errors='strict'), 'gb18030'


def normalize(data, encoding=None):
    text, detected = decode(data, encoding)
    if '\ufffd' in text or '\x00' in text:
        raise ValueError('CSV already contains replacement characters/NUL; restore original or specify source encoding')
    rows = [row for row in csv.reader(io.StringIO(text, newline=''), strict=True) if row]
    if not rows or not rows[0] or len(rows[0]) != len(set(rows[0])):
        raise ValueError('CSV has an empty/duplicate header')
    if any(len(row) != len(rows[0]) for row in rows):
        raise ValueError('CSV row width does not match its header; original preserved')
    # Normalize doubled CR/blank physical lines from Windows text export as well.
    # Cell values, embedded cell newlines and row order remain unchanged.
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\r\n').writerows(rows)
    result = stream.getvalue().encode('utf-8-sig')
    if list(csv.reader(io.StringIO(result.decode('utf-8-sig'), newline=''), strict=True)) != rows:
        raise ValueError('CSV round-trip mismatch')
    return result, {'source_encoding': detected, 'rows': len(rows) - 1, 'columns': len(rows[0])}


def convert(source, target=None, in_place=False, encoding=None):
    source = Path(source).resolve()
    target = source if target is None else Path(target).resolve()
    same = source == target
    if same and not in_place:
        raise ValueError('Use --in-place explicitly, or choose a new --output')
    if not same and target.exists():
        raise ValueError('Output already exists; preserved')
    metadata = []
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.csv-repair-', dir=target.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        if source.suffix.lower() == '.zip':
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(temp, 'w') as output:
                output.comment = original.comment
                for entry in original.infolist():
                    data = original.read(entry)
                    if not entry.is_dir() and entry.filename.lower().endswith('.csv'):
                        data, details = normalize(data, encoding)
                        metadata.append(dict(details, file=entry.filename))
                    output.writestr(entry, data)
            with zipfile.ZipFile(temp) as checked:
                if checked.testzip():
                    raise ValueError('Repaired ZIP verification failed')
        else:
            data, details = normalize(source.read_bytes(), encoding)
            temp.write_bytes(data)
            metadata.append(dict(details, file=source.name))
        if same:
            original_bytes = source.read_bytes()
            if temp.read_bytes() == original_bytes:
                return {'action': 'unchanged', 'files': metadata, 'new_calls': 0}
            backup = source.with_name(source.name + '.before-encoding-fix.bak')
            # Do not overwrite an earlier recovery copy.
            if not backup.exists():
                with backup.open('xb') as out:
                    out.write(original_bytes)
            os.replace(temp, target)
        elif os.name == 'nt':
            os.rename(temp, target)
        else:
            os.link(temp, target)
        return {'action': 'normalized', 'output': str(target), 'files': metadata, 'new_calls': 0}
    finally:
        temp.unlink(missing_ok=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--output')
    parser.add_argument('--in-place', action='store_true')
    parser.add_argument('--encoding', help='Explicit source encoding if automatic detection is ambiguous')
    args = parser.parse_args()
    try:
        print(json.dumps(convert(args.source, args.output, args.in_place, args.encoding), ensure_ascii=False))
        return 0
    except (ValueError, OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        print(json.dumps({'action': 'error', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
