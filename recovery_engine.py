from __future__ import annotations
import os, sys, struct, json, hashlib, uuid, math, copy, traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import py_aep
import py_aep.binary.chunk as ch
from py_aep.binary.chunk import Chunk, ListChunk, DeferredListChunk, read_aep, write_aep, read_bytes, read_fmt, write_chunk, EMPTY_CTX, ReadContext
from py_aep.binary.scalar_chunks import _StringChunkBase
from py_aep.models import (Application, Project, CompItem, FootageItem, FolderItem, AVLayer, TextLayer, ShapeLayer, CameraLayer, LightLayer, SolidSource, FileSource, PlaceholderSource)
import py_aep.parsers.effect as effect_module
import py_aep.parsers.project as project_module
import py_aep.binary.utils as bin_utils

_PATCHED = False

def _install_tolerant_reader():
    """Install resilient binary parser hooks for damaged After Effects projects."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def scalar_safe(cls, fp, size, *, chunk_type='', **kwargs):
        raw = read_bytes(fp, size)
        enc = getattr(cls, '_ENCODING', 'utf-8')
        try:
            return cls(chunk_type=chunk_type, value=raw.decode(enc, errors='replace'))
        except Exception:
            return cls(chunk_type=chunk_type, value=raw.decode('latin1', errors='replace'))
    _StringChunkBase.read = classmethod(scalar_safe)

    def tolerant_read_header(fp):
        raw_type = read_bytes(fp, 4)
        (len_body,) = read_fmt("I", fp)
        chunk_type = raw_type.decode("ASCII", errors="replace")
        return chunk_type, len_body
    ch.read_header = tolerant_read_header

    @classmethod
    def tolerant_list_chunk_read(cls, fp, size, *, chunk_type="", ctx=None, defer_list_types=None, **kwargs):
        if size < 4:
            raise OSError(f"{chunk_type} body too small: {size}")
        if ctx is None:
            ctx = EMPTY_CTX
        raw_lt = read_fmt("4s", fp)[0]
        list_type = raw_lt.decode("ASCII", errors="replace")
        if list_type == "btdk":
            data = read_bytes(fp, size - 4)
            return cls(list_type=list_type, data=data, chunk_type=chunk_type)
        parent_result = kwargs.get("parent_result")
        child_ctx = ReadContext(parent_list_type=list_type, grandparent_list_type=ctx.parent_list_type, parent_siblings=parent_result)
        if defer_list_types and list_type in defer_list_types:
            raw_body = fp.read(size - 4)
            return DeferredListChunk(chunk_type=chunk_type, list_type=list_type, raw_body=raw_body, raw_ctx=child_ctx)
        chunks = ch.read_chunks(fp, size - 4, ctx=child_ctx, defer_list_types=defer_list_types)
        return cls(list_type=list_type, chunks=chunks, chunk_type=chunk_type)
    ListChunk.read = tolerant_list_chunk_read

    known = set(ch.CHUNK_TYPES) | {'LIST', 'RIFX'}
    def plausible(raw: bytes, n: int, remaining: int) -> bool:
        try:
            t = raw.decode('ascii')
        except Exception:
            return False
        if len(t) != 4 or not all(32 <= ord(c) < 127 for c in t):
            return False
        if n < 0 or n > remaining:
            return False
        return t in known or all(c.isalnum() or c in '!_?@#$%&*' for c in t)

    def tolerant_read_chunks(fp, size, ctx=EMPTY_CTX, defer_list_types=None):
        start = fp.tell()
        end = start + size
        result = []
        while fp.tell() < end:
            pos = fp.tell()
            remain = end - pos
            if remain < 8:
                fp.seek(end)
                break
            raw = fp.read(4)
            try:
                n = struct.unpack('>I', fp.read(4))[0]
            except struct.error:
                fp.seek(end)
                break
            if plausible(raw, n, remain - 8):
                typ = raw.decode('ascii', errors='replace')
                cls = ch.CHUNK_TYPES.get(typ, Chunk)
                try:
                    item = cls.read(fp, n, chunk_type=typ, ctx=ctx, defer_list_types=defer_list_types)
                    result.append(item)
                    continue
                except Exception:
                    fp.seek(pos)
            scan = pos + 1
            found = False
            while scan + 8 <= end:
                fp.seek(scan)
                candidate = fp.read(4)
                try:
                    candidate_n = struct.unpack('>I', fp.read(4))[0]
                except struct.error:
                    break
                if plausible(candidate, candidate_n, end - scan - 8):
                    fp.seek(scan)
                    found = True
                    break
                scan += 1
            if not found:
                fp.seek(end)
                break
        return result
    ch.read_chunks = tolerant_read_chunks

_install_tolerant_reader()


def _safe_parse(path):
    try:
        return py_aep.parse(path)
    except Exception:
        with open(path, 'rb') as fp:
            app = read_aep(fp)
        return app


def parse_salvaged(path):
    return _safe_parse(path)


def _project_items(project):
    try:
        return list(project.items)
    except Exception:
        return []


def project_summary(app):
    project = getattr(app, 'project', None)
    items = _project_items(project) if project else []
    comps = [x for x in items if isinstance(x, CompItem)]
    return {'items': len(items), 'compositions': len(comps), 'project': getattr(project, 'file', None)}


def _layer_type(layer):
    if isinstance(layer, TextLayer): return 'Text'
    if isinstance(layer, ShapeLayer): return 'Shape'
    if isinstance(layer, CameraLayer): return 'Camera'
    if isinstance(layer, LightLayer): return 'Light'
    return 'AVLayer'


def _layer_dict(layer, index=0):
    tr = getattr(layer, 'transform', None)
    def val(name, default=None):
        obj = getattr(tr, name, None) if tr else None
        return getattr(obj, 'value', obj) if obj is not None else default
    effects = []
    try:
        effects = [{'name': getattr(e, 'name', ''), 'match_name': getattr(e, 'match_name', '')} for e in layer.effects]
    except Exception:
        pass
    return {'index': index, 'name': getattr(layer, 'name', f'Layer {index}'), 'type': _layer_type(layer), 'in_point': getattr(layer, 'in_point', 0), 'out_point': getattr(layer, 'out_point', 0), 'start_time': getattr(layer, 'start_time', 0), 'transform': {'position': val('position', [0, 0]), 'scale': val('scale', [100, 100]), 'rotation': val('rotation', 0), 'opacity': val('opacity', 100)}, 'effects': effects}


def extract_full_project_preview(app, source_path=''):
    project = getattr(app, 'project', None)
    items = _project_items(project) if project else []
    result = {'source': source_path, 'summary': project_summary(app), 'compositions': [], 'footage': []}
    for item in items:
        if isinstance(item, CompItem):
            layers = []
            try:
                layers = [_layer_dict(layer, i + 1) for i, layer in enumerate(item.layers)]
            except Exception:
                pass
            result['compositions'].append({'name': getattr(item, 'name', 'Composition'), 'width': getattr(item, 'width', 0), 'height': getattr(item, 'height', 0), 'duration': getattr(item, 'duration', 0), 'frame_rate': getattr(item, 'frame_rate', 0), 'layers': layers})
        elif isinstance(item, FootageItem):
            src = getattr(item, 'main_source', None)
            result['footage'].append({'name': getattr(item, 'name', 'Footage'), 'width': getattr(item, 'width', 0), 'height': getattr(item, 'height', 0), 'frame_rate': getattr(item, 'frame_rate', 0), 'path': getattr(src, 'file', None) or getattr(src, 'path', None)})
    return result


def _read_bytes(path):
    return Path(path).read_bytes()


def _hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def recover(damaged_path, autosave_path=None, output_path=None):
    """Recover an AEP while keeping the damaged file's newer project data authoritative."""
    damaged = Path(damaged_path)
    output = Path(output_path or (damaged.parent / f'{damaged.stem}_RECOVERED.aep'))
    report = {'input': str(damaged), 'autosave': str(autosave_path) if autosave_path else None, 'output': str(output), 'input_sha256': _hash(damaged), 'steps': []}
    try:
        app = _safe_parse(str(damaged))
        report['steps'].append('Parsed damaged project with tolerant RIFX/chunk reader.')
        try:
            write_aep(app, str(output))
        except Exception:
            project = getattr(app, 'project', None)
            if project is None:
                raise
            project.save(str(output))
        report['status'] = 'recovered'
        report['output_sha256'] = _hash(output)
        report['summary'] = project_summary(app)
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
    return report
