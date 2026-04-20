//! Corpus test harness — decodes every file in the all-the-images corpus
//! with zen decoders (via zencodecs) and validates against reference output.
//!
//! Two validation modes per file:
//!   1. **Hash check**: BLAKE3 of decoded pixels vs reference decoder hash
//!      from the manifest. Catches compatibility regressions.
//!   2. **Visual regression**: zensim-regress comparison of decoded pixels
//!      vs original source image. Measures encode+decode quality with
//!      psychovisual diff analysis (error category, rounding bias, score).
//!
//! Usage:
//!   cargo run --release -- --corpus ../corpus
//!   cargo run --release -- --corpus ../corpus --format jpeg
//!   cargo run --release -- --corpus ../corpus --format png --verbose

use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::PathBuf;

use serde::Deserialize;
use zensim::Zensim;
use zensim_regress::{check_regression, RegressionTolerance};

// ── Manifest types ────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Manifest {
    files: Vec<FileEntry>,
    #[serde(default)]
    sources: HashMap<String, SourceInfo>,
}

#[derive(Deserialize)]
struct FileEntry {
    path: String,
    #[allow(dead_code)]
    blake3: String,
    #[allow(dead_code)]
    bytes: u64,
    format: String,
    encoder: String,
    source: String,
    #[allow(dead_code)]
    params: serde_json::Value,
    #[serde(default)]
    reference_decodes: HashMap<String, String>,
}

#[derive(Deserialize)]
struct SourceInfo {
    width: u32,
    height: u32,
    channels: u32,
    #[serde(rename = "type")]
    source_type: String,
    #[serde(default = "default_bit_depth")]
    bit_depth: u32,
}

fn default_bit_depth() -> u32 { 8 }

fn bytemuck_cast_rgb(bytes: &[u8]) -> &[[u8; 3]] {
    assert!(bytes.len() % 3 == 0, "byte len {} not divisible by 3", bytes.len());
    // SAFETY: [u8] with alignment 1 can be reinterpreted as [[u8; 3]]
    unsafe { std::slice::from_raw_parts(bytes.as_ptr().cast(), bytes.len() / 3) }
}

fn bytemuck_cast_rgba(bytes: &[u8]) -> &[[u8; 4]] {
    assert!(bytes.len() % 4 == 0, "byte len {} not divisible by 4", bytes.len());
    unsafe { std::slice::from_raw_parts(bytes.as_ptr().cast(), bytes.len() / 4) }
}

// ── Format-specific decoders ──────────────────────────────────────────────

fn decode_jpeg(data: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    let decoder = zenjpeg::decoder::Decoder::new();
    let result = decoder
        .decode(data, enough::Unstoppable)
        .map_err(|e| format!("zenjpeg: {e}"))?;
    let w = result.width();
    let h = result.height();
    let pixels = result.pixels_u8().ok_or("zenjpeg: no u8 pixels")?;
    Ok((pixels.to_vec(), w, h))
}

fn decode_png(data: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    let config = zenpng::PngDecodeConfig::default();
    let output = zenpng::decode(data, &config, &enough::Unstoppable)
        .map_err(|e| format!("zenpng: {e}"))?;
    let w = output.info.width;
    let h = output.info.height;
    let bytes = output.pixels.into_vec();
    Ok((bytes, w, h))
}

fn decode_gif(data: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    let limits = zengif::Limits::default();
    let (metadata, frames, _stats) =
        zengif::decode_gif(data, limits, &enough::Unstoppable)
            .map_err(|e| format!("zengif: {e}"))?;
    let frame = frames.first().ok_or("zengif: no frames")?;
    let w = metadata.width as u32;
    let h = metadata.height as u32;
    // ComposedFrame has RGBA pixels — convert to RGB for comparison
    let rgba = &frame.pixels;
    let mut rgb = Vec::with_capacity((w * h * 3) as usize);
    for pixel in rgba {
        rgb.push(pixel.r);
        rgb.push(pixel.g);
        rgb.push(pixel.b);
    }
    Ok((rgb, w, h))
}

fn decode_format(format: &str, data: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    match format {
        "jpeg" => decode_jpeg(data),
        "png" => decode_png(data),
        "gif" => decode_gif(data),
        _ => Err(format!("no zen decoder for '{format}' yet")),
    }
}

// ── Source image loading ──────────────────────────────────────────────────

/// Load a PPM/PGM source image and return raw RGB(A) u8 pixels + dimensions.
fn load_source_pixels(
    corpus_dir: &std::path::Path,
    source_name: &str,
    source_info: &SourceInfo,
) -> Option<(Vec<u8>, u32, u32)> {
    // Try PPM (RGB) first, then PGM (grayscale)
    let sources_dir = corpus_dir.join("sources");
    let exts = if source_info.channels == 1 {
        vec!["pgm", "ppm"]
    } else {
        vec!["ppm", "png", "pfm"]
    };

    for ext in &exts {
        let path = sources_dir.join(format!("{source_name}.{ext}"));
        if !path.exists() {
            continue;
        }

        let mut data = Vec::new();
        fs::File::open(&path).ok()?.read_to_end(&mut data).ok()?;

        if *ext == "pfm" {
            // PFM is float — skip for now, zensim needs u8
            return None;
        }

        // Parse PNM header: P5/P6\nW H\nMAXVAL\n<pixels>
        if data.len() < 8 {
            return None;
        }
        let header = std::str::from_utf8(&data[..data.len().min(200)]).ok()?;
        let mut lines = header.lines();
        let magic = lines.next()?;
        if magic != "P5" && magic != "P6" {
            // Not a PNM — skip non-PNM sources for now
            return None;
        }

        // Skip comment lines
        let mut dim_line = lines.next()?;
        while dim_line.starts_with('#') {
            dim_line = lines.next()?;
        }
        let parts: Vec<&str> = dim_line.split_whitespace().collect();
        let w: u32 = parts.first()?.parse().ok()?;
        let h: u32 = parts.get(1)?.parse().ok()?;
        let maxval_line = lines.next()?;
        let _maxval: u32 = maxval_line.trim().parse().ok()?;

        // Find pixel data start
        let header_str = format!("{magic}\n{dim_line}\n{maxval_line}\n");
        let pixel_start = header_str.len();
        if pixel_start >= data.len() {
            return None;
        }

        let pixels = &data[pixel_start..];

        // If grayscale (P5), expand to RGB for zensim compatibility
        if magic == "P5" {
            let mut rgb = Vec::with_capacity(pixels.len() * 3);
            for &g in pixels {
                rgb.push(g);
                rgb.push(g);
                rgb.push(g);
            }
            return Some((rgb, w, h));
        }

        return Some((pixels.to_vec(), w, h));
    }
    None
}

// ── Per-format stats ──────────────────────────────────────────────────────

#[derive(Default)]
struct FormatStats {
    total: u32,
    decoded: u32,
    decode_errors: u32,
    hash_matches: u32,
    hash_mismatches: u32,
    no_reference: u32,
    zensim_scores: Vec<f64>,
    categories: HashMap<String, u32>,
}

impl FormatStats {
    fn median_score(&self) -> f64 {
        if self.zensim_scores.is_empty() {
            return 0.0;
        }
        let mut s = self.zensim_scores.clone();
        s.sort_by(|a, b| a.partial_cmp(b).unwrap());
        s[s.len() / 2]
    }

    fn min_score(&self) -> f64 {
        self.zensim_scores.iter().copied()
            .min_by(|a, b| a.partial_cmp(b).unwrap())
            .unwrap_or(0.0)
    }
}

// ── Main ──────────────────────────────────────────────────────────────────

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut corpus_dir = PathBuf::from("../corpus");
    let mut format_filter: Option<String> = None;
    let mut verbose = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--corpus" | "-c" => { i += 1; corpus_dir = PathBuf::from(&args[i]); }
            "--format" | "-f" => { i += 1; format_filter = Some(args[i].clone()); }
            "--verbose" | "-v" => { verbose = true; }
            "--help" | "-h" => {
                println!("Usage: corpus-test-harness [--corpus PATH] [--format FMT] [--verbose]");
                return;
            }
            other => {
                eprintln!("Unknown arg: {other}");
                std::process::exit(1);
            }
        }
        i += 1;
    }

    let manifest_path = corpus_dir.join("manifest.json");
    let manifest_data = fs::read_to_string(&manifest_path)
        .unwrap_or_else(|e| panic!("Can't read {}: {e}", manifest_path.display()));
    let manifest: Manifest = serde_json::from_str(&manifest_data)
        .unwrap_or_else(|e| panic!("Can't parse manifest: {e}"));

    println!("Corpus: {}", corpus_dir.display());
    println!("Files:  {}", manifest.files.len());
    println!("Sources: {}", manifest.sources.len());

    let zensim = Zensim::new(zensim::ZensimProfile::PreviewV0_2);

    let mut stats: HashMap<String, FormatStats> = HashMap::new();
    let total_files = manifest.files.len();
    let mut processed = 0u32;

    for entry in &manifest.files {
        if let Some(ref ff) = format_filter {
            if &entry.format != ff {
                continue;
            }
        }

        processed += 1;
        let fs = stats.entry(entry.format.clone()).or_default();
        fs.total += 1;

        let file_path = corpus_dir.join(&entry.path);
        let data = match fs::read(&file_path) {
            Ok(d) => d,
            Err(_) => { continue; }
        };

        // ── Decode with zen decoder ──
        let (zen_pixels, zen_w, zen_h) = match decode_format(&entry.format, &data) {
            Ok(r) => r,
            Err(e) => {
                fs.decode_errors += 1;
                if fs.decode_errors <= 5 || verbose {
                    eprintln!("DECODE_ERR [{}/{}] {}: {}",
                        entry.format, entry.encoder, entry.path, e);
                }
                continue;
            }
        };
        fs.decoded += 1;

        // ── Hash comparison vs reference decoders ──
        let zen_hash = blake3::hash(&zen_pixels).to_hex().to_string();
        let mut hash_matched = false;
        if !entry.reference_decodes.is_empty() {
            for (_ref_name, ref_hash) in &entry.reference_decodes {
                if zen_hash == *ref_hash {
                    hash_matched = true;
                    break;
                }
            }
            if hash_matched {
                fs.hash_matches += 1;
            } else {
                fs.hash_mismatches += 1;
            }
        } else {
            fs.no_reference += 1;
        }

        // ── Visual regression vs source image (zensim) ──
        let source_info = manifest.sources.get(&entry.source);
        if let Some(si) = source_info {
            // Skip HDR/16-bit sources — zensim needs u8
            if si.bit_depth > 8 {
                continue;
            }

            if let Some((src_pixels, src_w, src_h)) =
                load_source_pixels(&corpus_dir, &entry.source, si)
            {
                // Dimensions must match for direct comparison
                let zen_channels = zen_pixels.len() as u32 / (zen_w * zen_h);
                let src_channels = src_pixels.len() as u32 / (src_w * src_h);

                if zen_w == src_w && zen_h == src_h && zen_channels == src_channels
                    && zen_w >= 8 && zen_h >= 8 // zensim needs 8x8 minimum
                {
                    let tolerance = RegressionTolerance::off_by_one()
                        .with_min_similarity(0.0); // Don't fail — just measure

                    let report_result = if zen_channels == 3 {
                        // Safety: &[u8] with len = w*h*3 can be viewed as &[[u8; 3]]
                        let src_rgb: &[[u8; 3]] = bytemuck_cast_rgb(&src_pixels);
                        let zen_rgb: &[[u8; 3]] = bytemuck_cast_rgb(&zen_pixels);
                        check_regression(
                            &zensim,
                            &zensim::RgbSlice::new(src_rgb, src_w as usize, src_h as usize),
                            &zensim::RgbSlice::new(zen_rgb, zen_w as usize, zen_h as usize),
                            &tolerance,
                        )
                    } else if zen_channels == 4 {
                        let src_rgba: &[[u8; 4]] = bytemuck_cast_rgba(&src_pixels);
                        let zen_rgba: &[[u8; 4]] = bytemuck_cast_rgba(&zen_pixels);
                        check_regression(
                            &zensim,
                            &zensim::RgbaSlice::new(src_rgba, src_w as usize, src_h as usize),
                            &zensim::RgbaSlice::new(zen_rgba, zen_w as usize, zen_h as usize),
                            &tolerance,
                        )
                    } else {
                        continue;
                    };

                    if let Ok(report) = report_result {
                        fs.zensim_scores.push(report.score());
                        let cat = format!("{:?}", report.category());
                        *fs.categories.entry(cat).or_default() += 1;

                        if verbose && !hash_matched && !entry.reference_decodes.is_empty() {
                            eprintln!(
                                "  DIFF {}: score={:.1} cat={:?} max_delta={:?} pixels_diff={}",
                                entry.path,
                                report.score(),
                                report.category(),
                                report.max_channel_delta(),
                                report.pixels_differing(),
                            );
                        }
                    }
                }
            }
        }

        if processed % 200 == 0 {
            eprint!("\r  [{processed}/{total_files}]");
        }
    }
    eprintln!();

    // ── Summary ──
    println!();
    println!("═══════════════════════════════════════════════════════════");
    println!("  Format  Total  Decoded  Errors  HashOK  Mismatch  NoRef");
    println!("═══════════════════════════════════════════════════════════");

    let mut all_decoded = 0u32;
    let mut all_errors = 0u32;
    let mut all_match = 0u32;
    let mut all_mismatch = 0u32;

    for fmt in ["jpeg", "png", "webp", "gif", "avif", "jxl", "tiff", "heic"] {
        if let Some(fs) = stats.get(fmt) {
            println!(
                "  {fmt:6}  {total:5}  {decoded:7}  {errors:6}  {ok:6}  {mis:8}  {noref:5}",
                total = fs.total,
                decoded = fs.decoded,
                errors = fs.decode_errors,
                ok = fs.hash_matches,
                mis = fs.hash_mismatches,
                noref = fs.no_reference,
            );
            all_decoded += fs.decoded;
            all_errors += fs.decode_errors;
            all_match += fs.hash_matches;
            all_mismatch += fs.hash_mismatches;
        }
    }
    println!("───────────────────────────────────────────────────────────");
    println!(
        "  total   {processed:5}  {all_decoded:7}  {all_errors:6}  {all_match:6}  {all_mismatch:8}"
    );

    // ── Zensim quality analysis ──
    println!();
    println!("═══ Visual Quality (zensim vs source) ═══");
    for fmt in ["jpeg", "png", "webp", "gif", "avif", "jxl", "tiff", "heic"] {
        if let Some(fs) = stats.get(fmt) {
            if !fs.zensim_scores.is_empty() {
                println!(
                    "  {fmt:6}: n={n:5}  median={med:6.1}  min={min:6.1}",
                    n = fs.zensim_scores.len(),
                    med = fs.median_score(),
                    min = fs.min_score(),
                );

                // Error categories
                if !fs.categories.is_empty() {
                    let mut cats: Vec<_> = fs.categories.iter().collect();
                    cats.sort_by(|a, b| b.1.cmp(a.1));
                    for (cat, count) in &cats {
                        println!("           {cat:20} {count:5}");
                    }
                }
            }
        }
    }

    // ── Exit code ──
    if all_errors > 0 {
        println!();
        println!("FAIL: {all_errors} decode errors");
        std::process::exit(1);
    }
}
