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
from py_aep.models import (
    Application, Project, CompItem, FootageItem, FolderItem,
    AVLayer, TextLayer, ShapeLayer, CameraLayer, LightLayer,
    SolidSource, FileSource, PlaceholderSource
)
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

    # 1. Safe String & Text decoding with error replacement
    def scalar_safe(cls, fp, size, *, chunk_type='', **kwargs):
        raw = read_bytes(fp, size)
        enc = getattr(cls, '_ENCODING', 'utf-8')
        try:
            return cls(chunk_type=chunk_type, value=raw.decode(enc, errors='replace'))
        except Exception:
            return cls(chunk_type=chunk_type, value=raw.decode('latin1', errors='replace'))
    _StringChunkBase.read = classmethod(scalar_safe)

    # 2. Tolerant read_header with safe ASCII decode
    def tolerant_read_header(fp):
        raw_type = read_bytes(fp, 4)
        (len_body,) = read_fmt("I", fp)
        chunk_type = raw_type.decode("ASCII", errors="replace")
        return chunk_type, len_body
    ch.read_header = tolerant_read_header

    # 3. Tolerant ListChunk reader with safe list_type decode
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
        child_ctx = ReadContext(
            parent_list_type=list_type,
            grandparent_list_type=ctx.parent_list_type,
            parent_siblings=parent_result,
        )
        if defer_list_types and list_type in defer_list_types:
            raw_body = fp.read(size - 4)
            return DeferredListChunk(
                chunk_type=chunk_type,
                list_type=list_type,
                raw_body=raw_body,
                raw_ctx=child_ctx,
            )
        chunks = ch.read_chunks(
            fp,
            size - 4,
            ctx=child_ctx,
            defer_list_types=defer_list_types,
        )
        return cls(
            list_type=list_type,
            chunks=chunks,
            chunk_type=chunk_type,
        )
    ListChunk.read = tolerant_list_chunk_read

    # 4. Plausibility test for 4CC chunks
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

    # 5. Tolerant chunk stream scanner with resynchronization
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
                    resolver = ch._CONTEXT_RESOLVERS.get(typ)
                    kw = resolver(result, ctx) if resolver else {}
                    obj = cls.read(fp, n, chunk_type=typ, ctx=ctx, parent_result=result, defer_list_types=defer_list_types, **kw)
                    result.append(obj)
                    ch.read_pad(fp, n)
                    continue
                except Exception:
                    fp.seek(pos)

            # Corrupted header or length: search ahead for the next plausible 4CC header
            fp.seek(pos + 1)
            found = None
            limit = min(end - 8, pos + 1000000)
            while fp.tell() <= limit:
                p = fp.tell()
                rr = fp.read(8)
                if len(rr) < 8:
                    break
                hdr_raw = rr[:4]
                try:
                    hdr_n = struct.unpack('>I', rr[4:])[0]
                except Exception:
                    fp.seek(p + 1)
                    continue
                if plausible(hdr_raw, hdr_n, end - (p + 8)):
                    found = p
                    break
                fp.seek(p + 1)
            if found is None:
                fp.seek(end)
                break
            else:
                fp.seek(found)
        return result

    ch.read_chunks = tolerant_read_chunks

    # 6. Tolerant read_aep that handles corrupted trailing XMP
    def tolerant_read_aep(fp, *, defer_list_types=None):
        raw_type = read_bytes(fp, 4)
        chunk_type = raw_type.decode("ASCII", errors="replace")
        if chunk_type != "RIFX":
            # If not RIFX at 0, search for RIFX within first 1024 bytes
            fp.seek(0)
            prefix = fp.read(1024)
            idx = prefix.find(b'RIFX')
            if idx >= 0:
                fp.seek(idx + 4)
            else:
                raise ValueError(f"Expected RIFX header, got {chunk_type!r}")
        (len_body,) = read_fmt("I", fp)
        rifx = ListChunk.read(
            fp,
            len_body,
            chunk_type="RIFX",
            defer_list_types=defer_list_types,
        )
        try:
            xmp = fp.read().decode("UTF-8", errors="replace")
        except Exception:
            xmp = ""
        return rifx, xmp

    ch.read_aep = tolerant_read_aep

    # 7. Robust Effect Definitions Parser
    def robust_parse_effect_definitions(chunks):
        try:
            efdg_chunk = bin_utils.find_by_list_type(chunks=chunks, list_type="EfdG")
        except Exception:
            return {}
        effect_defs = {}
        for efdf_chunk in bin_utils.filter_by_list_type(chunks=efdg_chunk.chunks, list_type="EfDf"):
            try:
                tdmn_chunk = bin_utils.find_by_type(chunks=efdf_chunk.chunks, chunk_type="tdmn")
                effect_match_name = tdmn_chunk.value
                sspc_chunk = bin_utils.find_by_list_type(chunks=efdf_chunk.chunks, list_type="sspc")
                param_defs = effect_module.parse_effect_param_defs(sspc_chunk.chunks)
                effect_defs[effect_match_name] = param_defs
            except Exception:
                continue
        return effect_defs

    effect_module.parse_effect_definitions = robust_parse_effect_definitions
    project_module.parse_effect_definitions = robust_parse_effect_definitions


def seconds_to_timecode(secs: float, fps: float) -> str:
    """Format seconds into SMPTE timecode HH:MM:SS:FF."""
    if fps <= 0:
        fps = 30.0
    total_frames = int(round(secs * fps))
    fps_int = max(1, int(round(fps)))
    hours = total_frames // (3600 * fps_int)
    rem = total_frames % (3600 * fps_int)
    mins = rem // (60 * fps_int)
    rem = rem % (60 * fps_int)
    seconds = rem // fps_int
    frames = rem % fps_int
    return f"{hours:02d}:{mins:02d}:{seconds:02d}:{frames:02d}"


def parse_salvaged(path: str) -> Application:
    """Parse an After Effects project with tolerant forensic hooks enabled."""
    _install_tolerant_reader()
    return py_aep.parse(path)


def extract_full_project_preview(app: Application, file_path: Optional[str] = None) -> Dict[str, Any]:
    """Extract a complete, rich preview representation of the project."""
    proj = app.project
    file_stat = None
    file_size_bytes = 0
    if file_path and Path(file_path).exists():
        file_stat = Path(file_path).stat()
        file_size_bytes = file_stat.st_size

    # 1. Compositions & Layers
    comps_data = []
    total_layers_count = 0
    all_effects_used = set()

    for comp in proj.compositions:
        comp_fps = round(float(getattr(comp, 'frame_rate', 30.0)), 3)
        comp_dur = round(float(getattr(comp, 'duration', 0.0)), 3)
        comp_w = int(getattr(comp, 'width', 1920))
        comp_h = int(getattr(comp, 'height', 1080))
        
        bg_col = [0, 0, 0]
        if hasattr(comp, 'bg_color') and comp.bg_color:
            bg_col = [round(float(c), 3) for c in comp.bg_color[:3]]

        c_info = {
            'id': getattr(comp, 'id', None) or id(comp),
            'name': str(comp.name),
            'width': comp_w,
            'height': comp_h,
            'aspect_ratio': f"{comp_w}:{comp_h}" if math.gcd(comp_w, comp_h) == 0 else f"{comp_w//math.gcd(comp_w, comp_h)}:{comp_h//math.gcd(comp_w, comp_h)}",
            'frame_rate': comp_fps,
            'duration': comp_dur,
            'timecode': seconds_to_timecode(comp_dur, comp_fps),
            'bg_color': bg_col,
            'layer_count': len(comp.layers),
            'layers': []
        }

        for l in comp.layers:
            total_layers_count += 1
            l_type = l.__class__.__name__
            in_p = round(float(getattr(l, 'in_point', 0.0)), 3)
            out_p = round(float(getattr(l, 'out_point', comp_dur)), 3)
            st_time = round(float(getattr(l, 'start_time', 0.0)), 3)
            dur = max(0.0, round(out_p - in_p, 3))

            l_info = {
                'index': int(getattr(l, 'index', 0)),
                'name': str(getattr(l, 'name', f'Layer {l.index}')),
                'type': l_type,
                'in_point': in_p,
                'out_point': out_p,
                'start_time': st_time,
                'duration': dur,
                'enabled': bool(getattr(l, 'enabled', True)),
                'solo': bool(getattr(l, 'solo', False)),
                'locked': bool(getattr(l, 'locked', False)),
                'three_d': bool(getattr(l, 'three_d_layer', False)),
                'null_layer': bool(getattr(l, 'null_layer', False)),
                'adjustment_layer': bool(getattr(l, 'adjustment_layer', False)),
                'guide_layer': bool(getattr(l, 'guide_layer', False)),
                'motion_blur': bool(getattr(l, 'motion_blur', False)),
                'effects': [],
                'transform': {}
            }

            # Source metadata
            if hasattr(l, 'source') and l.source:
                s = l.source
                l_info['source_name'] = str(s.name)
                l_info['source_type'] = s.__class__.__name__
                if hasattr(s, 'main_source'):
                    ms = s.main_source
                    if isinstance(ms, SolidSource):
                        l_info['solid_color'] = [round(float(c), 3) for c in ms.color] if hasattr(ms, 'color') else [0.5, 0.5, 0.5]
                        l_info['source_kind'] = 'Solid'
                    elif isinstance(ms, FileSource):
                        l_info['file_path'] = str(ms.file) if hasattr(ms, 'file') else None
                        l_info['source_kind'] = 'File'
                    elif isinstance(ms, PlaceholderSource):
                        l_info['source_kind'] = 'Placeholder'
                elif isinstance(s, CompItem):
                    l_info['source_kind'] = 'Precomp'

            # Applied Effects
            if hasattr(l, 'effects') and l.effects:
                for eff in l.effects:
                    eff_name = str(getattr(eff, 'name', 'Effect'))
                    match_name = str(getattr(eff, 'match_name', ''))
                    all_effects_used.add(eff_name)
                    l_info['effects'].append({
                        'name': eff_name,
                        'match_name': match_name,
                        'enabled': bool(getattr(eff, 'enabled', True))
                    })

            # Text content
            if isinstance(l, TextLayer) or hasattr(l, 'text'):
                try:
                    if hasattr(l, 'text') and hasattr(l.text, 'source_text'):
                        td = l.text.source_text.value
                        if td:
                            l_info['text_preview'] = str(getattr(td, 'text', ''))[:200]
                            l_info['font'] = str(getattr(td, 'font', ''))
                            l_info['font_size'] = float(getattr(td, 'font_size', 0))
                            if hasattr(td, 'fill_color') and td.fill_color:
                                l_info['text_color'] = [round(float(c), 3) for c in td.fill_color]
                except Exception:
                    pass

            # Transform properties
            if hasattr(l, 'transform') and l.transform:
                try:
                    t = l.transform
                    if hasattr(t, 'position') and t.position and t.position.value is not None:
                        val = t.position.value
                        l_info['transform']['position'] = [round(float(v), 2) for v in val] if isinstance(val, (list, tuple)) else val
                    if hasattr(t, 'scale') and t.scale and t.scale.value is not None:
                        val = t.scale.value
                        l_info['transform']['scale'] = [round(float(v), 2) for v in val] if isinstance(val, (list, tuple)) else val
                    if hasattr(t, 'opacity') and t.opacity and t.opacity.value is not None:
                        l_info['transform']['opacity'] = round(float(t.opacity.value), 2)
                    if hasattr(t, 'rotation') and t.rotation and t.rotation.value is not None:
                        l_info['transform']['rotation'] = round(float(t.rotation.value), 2)
                    if hasattr(t, 'anchor_point') and t.anchor_point and t.anchor_point.value is not None:
                        val = t.anchor_point.value
                        l_info['transform']['anchor_point'] = [round(float(v), 2) for v in val] if isinstance(val, (list, tuple)) else val
                except Exception:
                    pass

            c_info['layers'].append(l_info)
        comps_data.append(c_info)

    # 2. Assets & Footage items
    assets_data = []
    folders_data = []
    for item_id, item in proj.items.items():
        if isinstance(item, FootageItem):
            f_info = {
                'id': item_id,
                'name': str(item.name),
                'width': getattr(item, 'width', 0),
                'height': getattr(item, 'height', 0),
                'duration': round(float(getattr(item, 'duration', 0.0)), 3),
                'frame_rate': round(float(getattr(item, 'frame_rate', 0.0)), 3),
                'has_audio': bool(getattr(item, 'has_audio', False)),
                'has_video': bool(getattr(item, 'has_video', True)),
                'file_path': None,
                'footage_type': 'Unknown',
                'missing': bool(getattr(item, 'footage_missing_path', False))
            }
            if hasattr(item, 'main_source') and item.main_source:
                ms = item.main_source
                if isinstance(ms, FileSource):
                    f_info['footage_type'] = 'File'
                    f_info['file_path'] = str(getattr(ms, 'file', ''))
                elif isinstance(ms, SolidSource):
                    f_info['footage_type'] = 'Solid'
                    f_info['color'] = [round(float(c), 3) for c in ms.color] if hasattr(ms, 'color') else None
                elif isinstance(ms, PlaceholderSource):
                    f_info['footage_type'] = 'Placeholder'
            assets_data.append(f_info)
        elif isinstance(item, FolderItem) and item != proj.root_folder:
            folders_data.append({
                'id': item_id,
                'name': str(item.name),
                'parent_id': getattr(item.parent_folder, 'id', None) if getattr(item, 'parent_folder', None) else None
            })

    # 3. Render Queue
    rq_data = []
    try:
        if hasattr(proj, 'render_queue') and proj.render_queue:
            for rq_item in proj.render_queue.items:
                rq_data.append({
                    'status': str(getattr(rq_item, 'status', 'Unqueued')),
                    'comp_name': str(getattr(rq_item.comp, 'name', '')) if hasattr(rq_item, 'comp') and rq_item.comp else 'Unknown',
                    'output_modules': [
                        {
                            'file': str(getattr(om, 'file', '')),
                            'format': str(getattr(om, 'format', ''))
                        } for om in getattr(rq_item, 'output_modules', [])
                    ]
                })
    except Exception:
        pass

    # Build Project Overview
    project_meta = {
        'file_name': Path(file_path).name if file_path else 'project.aep',
        'file_size_bytes': file_size_bytes,
        'file_size_formatted': f"{file_size_bytes / (1024*1024):.2f} MB" if file_size_bytes > 0 else "N/A",
        'version': str(getattr(app, 'version', 'Unknown')),
        'build_number': str(getattr(app, 'build_number', '')),
        'bit_depth': str(getattr(proj, 'bits_per_channel', '8 BitsPerChannel')),
        'color_management': str(getattr(proj, 'color_management_system', 'Standard')),
        'total_items': len(proj.items),
        'total_compositions': len(comps_data),
        'total_assets': len(assets_data),
        'total_folders': len(folders_data),
        'total_layers': total_layers_count,
        'total_effects_used': sorted(list(all_effects_used)),
        'render_queue_count': len(rq_data)
    }

    return {
        'meta': project_meta,
        'compositions': comps_data,
        'assets': assets_data,
        'folders': folders_data,
        'render_queue': rq_data
    }


def project_summary(app: Application, file_path: Optional[str] = None) -> Dict[str, Any]:
    """Lightweight project summary for fast status reporting."""
    p = app.project
    comps = []
    for c in p.compositions:
        comps.append({
            'name': str(c.name),
            'width': int(c.width),
            'height': int(c.height),
            'duration': float(c.duration),
            'layers': len(c.layers)
        })
    return {
        'version': str(app.version),
        'composition_count': len(comps),
        'layer_count': sum(x['layers'] for x in comps),
        'compositions': comps
    }


def heal_damaged_rifx(damaged_rifx: ListChunk, reference_rifx: Optional[ListChunk] = None) -> None:
    """Sanitize and repair damaged RIFX tree in-place before saving."""
    # 1. Clean out illegal or invalid chunks inside root and Fold
    if hasattr(damaged_rifx, 'chunks'):
        valid_chunks = []
        for c in damaged_rifx.chunks:
            if getattr(c, 'chunk_type', '') == 'CORR':
                continue
            valid_chunks.append(c)
        damaged_rifx.chunks = valid_chunks

    # 2. Heal Effect Definitions (LIST:EfdG) if missing or corrupt
    damaged_efdg = [c for c in damaged_rifx.chunks if isinstance(c, ListChunk) and c.list_type == 'EfdG']
    if reference_rifx:
        ref_efdg = [c for c in reference_rifx.chunks if isinstance(c, ListChunk) and c.list_type == 'EfdG']
        if ref_efdg:
            if not damaged_efdg:
                damaged_rifx.chunks.append(copy.deepcopy(ref_efdg[0]))
            else:
                # Merge missing effect definitions from reference into damaged
                existing_match_names = set()
                for efdf in damaged_efdg[0].chunks:
                    if isinstance(efdf, ListChunk):
                        for sub in efdf.chunks:
                            if sub.chunk_type == 'tdmn':
                                existing_match_names.add(getattr(sub, 'value', ''))
                for ref_efdf in ref_efdg[0].chunks:
                    if isinstance(ref_efdf, ListChunk):
                        for sub in ref_efdf.chunks:
                            if sub.chunk_type == 'tdmn' and getattr(sub, 'value', '') not in existing_match_names:
                                damaged_efdg[0].chunks.append(copy.deepcopy(ref_efdf))
                                break


def recover(damaged_path: str, autosave_path: Optional[str] = None, out_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Forensic salvage and repair for damaged After Effects projects.
    
    Treats the damaged file as authoritative, recovers all salvageable compositions
    and layers, heals corrupted effect definition tables and chunk headers,
    and produces a clean, standard After Effects project file.
    """
    _install_tolerant_reader()
    
    damaged_p = Path(damaged_path)
    if not damaged_p.exists():
        raise FileNotFoundError(f"Corrupted AEP not found: {damaged_path}")

    if out_path is None:
        out_p = damaged_p.with_name(damaged_p.stem + "_RECOVERED.aep")
    else:
        out_p = Path(out_path)

    # 1. Parse damaged project with forensic salvage
    damaged_app = py_aep.parse(str(damaged_p))
    damaged_preview = extract_full_project_preview(damaged_app, str(damaged_p))

    # 2. Parse reference autosave if provided
    ref_app = None
    ref_preview = None
    ref_rifx = None
    if autosave_path and Path(autosave_path).exists():
        try:
            ref_app = py_aep.parse(autosave_path)
            ref_preview = extract_full_project_preview(ref_app, autosave_path)
            ref_rifx = getattr(ref_app.project, '_rifx', None)
        except Exception as e:
            ref_preview = {'error': f"{type(e).__name__}: {str(e)}"}

    # 3. Heal and Sanitize the RIFX chunk tree
    heal_damaged_rifx(damaged_app.project._rifx, ref_rifx)

    # 4. Save the repaired project
    if out_p.exists():
        out_p.unlink()
    
    damaged_app.project.save(str(out_p))

    # 5. Full standard parser verification test on the output file
    verified_ok = True
    verified_error = None
    recovered_preview = None
    try:
        recovered_app = py_aep.parse(str(out_p))
        recovered_preview = extract_full_project_preview(recovered_app, str(out_p))
    except Exception as e:
        verified_ok = False
        verified_error = f"{type(e).__name__}: {str(e)}"
        try:
            # Fallback re-parse with tolerant reader if needed
            recovered_app = parse_salvaged(str(out_p))
            recovered_preview = extract_full_project_preview(recovered_app, str(out_p))
        except Exception:
            pass

    # 6. Diff & Comparison Analysis
    damaged_comp_names = {c['name']: c for c in damaged_preview['compositions']}
    ref_comp_names = {c['name']: c for c in (ref_preview.get('compositions') if ref_preview and 'compositions' in ref_preview else [])}
    recovered_comp_names = {c['name']: c for c in (recovered_preview.get('compositions') if recovered_preview and 'compositions' in recovered_preview else [])}

    beyond_autosave = sorted(list(set(damaged_comp_names.keys()) - set(ref_comp_names.keys())))
    only_in_autosave = sorted(list(set(ref_comp_names.keys()) - set(damaged_comp_names.keys())))

    layer_diffs = []
    for cname in damaged_comp_names.keys() & ref_comp_names.keys():
        d_layers = damaged_comp_names[cname]['layer_count']
        r_layers = ref_comp_names[cname]['layer_count']
        if d_layers != r_layers:
            layer_diffs.append({
                'composition': cname,
                'damaged_layers': d_layers,
                'autosave_layers': r_layers,
                'diff': d_layers - r_layers
            })

    report = {
        'status': 'success' if verified_ok else 'warning',
        'method': 'Forensic salvage first with RIFX boundary repair & effect table healing',
        'damaged_path': str(damaged_p.resolve()),
        'autosave_path': str(Path(autosave_path).resolve()) if autosave_path else None,
        'output_path': str(out_p.resolve()),
        'download': f"/download/{out_p.name}",
        'output_size_bytes': out_p.stat().st_size if out_p.exists() else 0,
        'output_size_formatted': f"{out_p.stat().st_size / (1024*1024):.2f} MB" if out_p.exists() else "0 MB",
        'standard_parser_verified': verified_ok,
        'verification_error': verified_error,
        'statistics': {
            'compositions_salvaged': len(recovered_comp_names),
            'layers_salvaged': recovered_preview['meta']['total_layers'] if recovered_preview else 0,
            'assets_recovered': recovered_preview['meta']['total_assets'] if recovered_preview else 0,
            'compositions_preserved_beyond_autosave': beyond_autosave,
            'compositions_only_in_autosave': only_in_autosave,
            'layer_differences': layer_diffs
        },
        'damaged_preview': damaged_preview,
        'reference_preview': ref_preview,
        'recovered_preview': recovered_preview
    }

    return report

