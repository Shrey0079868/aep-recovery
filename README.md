# AEP Recovery Lab & Project Preview Studio

A professional forensic recovery suite and interactive project inspector for Adobe After Effects (`.aep`) files.

> **Note**: This is a **vibecoded** project created with AI assistance, with the entire concept, architectural design, forensic recovery strategy, and technical orchestration directed and orchestrated by **[@Shrey0079868](https://github.com/Shrey0079868)**.

Treats the **damaged AEP as authoritative** to preserve all recent work (compositions, layers, effects, transforms) beyond old autosaves, while repairing corrupt chunk boundaries and healing effect tables so the recovered project opens cleanly in Adobe After Effects.

---

## Key Features

1. **Interactive Project Previewer**:
   - **Visual 2D Composition Canvas**: Real-time canvas rendering of layer layout, solid colors, text, and transform positions/scales/rotations.
   - **Timeline Scrubber & Playback**: Scrub through composition time with live SMPTE timecode (`HH:MM:SS:FF`) and layer in/out visibility.
   - **Deep Layer Inspector**: Visual Gantt duration bars, layer type badges (Text, Solid, Video, Shape, Camera, Light, Null, Adjustment), applied effects with match names, and transform properties.
   - **Assets & Footage Explorer**: Media file paths, dimensions, frame rates, and missing footage diagnostics.
   - **Standalone Inspector**: Inspect and preview any `.aep` file immediately without running recovery.

2. **Forensic Recovery Engine**:
   - Resilient ASCII / UTF-8 string decoding that tolerates corrupted byte sequences.
   - Chunk resynchronization without injecting illegal chunk types into standard RIFX containers.
   - Intelligent Effect Table (`LIST:EfdG`) healing that restores broken or missing effect descriptors.
   - Preserves compositions and layers created after the last autosave.
   - Automatic verification against the standard Adobe After Effects parser.

3. **Forensic Diff & Comparison**:
   - Side-by-side breakdown comparing Damaged vs. Autosave vs. Recovered project files.
   - Highlights preserved compositions and layer differences.

---

## Launching

### Windows (Recommended)
Run **`run_windows.bat`** or in terminal:
```bat
py -3 app.py
```

### macOS / Linux
```bash
python3 app.py
```

Then open `http://127.0.0.1:8765` in your browser.


