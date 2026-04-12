//! Corpus test harness — decodes every file in the all-the-images corpus
//! with the corresponding zen decoder and compares pixel hashes against
//! reference decoder output.
//!
//! Usage:
//!   cargo run --release -- --corpus ../corpus
//!   cargo run --release --features all -- --corpus ../corpus
//!   cargo run --release -- --corpus ../corpus --format jpeg

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

// ── Manifest types ────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Manifest {
    files: Vec<FileEntry>,
}

#[derive(Deserialize)]
struct FileEntry {
    path: String,
    blake3: String,
    bytes: u64,
    format: String,
    encoder: String,
    source: String,
    params: serde_json::Value,
    #[serde(default)]
    reference_decodes: HashMap<String, String>,
}

// ── Decoder wrappers ──────────────────────────────────────────────────────

/// Result of decoding: raw pixel bytes or an error message.
struct DecodeResult {
    pixels: Vec<u8>,
    width: u32,
    height: u32,
    channels: u32,
}

#[cfg(feature = "jpeg")]
fn decode_jpeg(data: &[u8]) -> Result<DecodeResult, String> {
    let decoder = zenjpeg::decoder::Decoder::new();
    let result = decoder
        .decode(data, enough::Unstoppable)
        .map_err(|e| format!("zenjpeg: {e}"))?;
    let w = result.width();
    let h = result.height();
    let pixels = result.pixels_u8().ok_or("zenjpeg: no u8 pixels")?;
    let channels = (pixels.len() as u32) / (w * h);
    Ok(DecodeResult {
        pixels: pixels.to_vec(),
        width: w,
        height: h,
        channels,
    })
}

// Other format decoders are added incrementally as compilation succeeds.
// Start with JPEG, add more as APIs are verified.

// ── Main ──────────────────────────────────────────────────────────────────

fn decode_file(format: &str, data: &[u8]) -> Result<DecodeResult, String> {
    match format {
        #[cfg(feature = "jpeg")]
        "jpeg" => decode_jpeg(data),
        _ => Err(format!("no decoder compiled for format '{format}'")),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut corpus_dir = PathBuf::from("../corpus");
    let mut format_filter: Option<String> = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--corpus" | "-c" => {
                i += 1;
                corpus_dir = PathBuf::from(&args[i]);
            }
            "--format" | "-f" => {
                i += 1;
                format_filter = Some(args[i].clone());
            }
            _ => {
                eprintln!("Usage: corpus-test-harness --corpus <path> [--format jpeg]");
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
    println!();

    let mut total = 0u32;
    let mut decoded_ok = 0u32;
    let mut decode_err = 0u32;
    let mut hash_match = 0u32;
    let mut hash_mismatch = 0u32;
    let mut no_reference = 0u32;
    let mut skipped = 0u32;

    // Track per-format stats
    let mut format_stats: HashMap<String, (u32, u32, u32, u32)> = HashMap::new();

    for entry in &manifest.files {
        if let Some(ref ff) = format_filter {
            if &entry.format != ff {
                continue;
            }
        }

        total += 1;
        let file_path = corpus_dir.join(&entry.path);
        let data = match fs::read(&file_path) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("SKIP {}: read error: {e}", entry.path);
                skipped += 1;
                continue;
            }
        };

        let stats = format_stats
            .entry(entry.format.clone())
            .or_insert((0, 0, 0, 0));
        stats.0 += 1; // total

        match decode_file(&entry.format, &data) {
            Ok(result) => {
                decoded_ok += 1;
                stats.1 += 1; // ok

                // Hash the decoded pixels
                let pixel_hash = blake3::hash(&result.pixels).to_hex().to_string();

                // Compare against any available reference decode
                let mut matched = false;
                let mut checked = false;
                for (ref_name, ref_hash) in &entry.reference_decodes {
                    checked = true;
                    if pixel_hash == *ref_hash {
                        matched = true;
                        break;
                    }
                }

                if !checked {
                    no_reference += 1;
                } else if matched {
                    hash_match += 1;
                    stats.2 += 1; // match
                } else {
                    hash_mismatch += 1;
                    stats.3 += 1; // mismatch
                    // Print first few mismatches
                    if hash_mismatch <= 20 {
                        let ref_names: Vec<_> = entry.reference_decodes.keys().collect();
                        eprintln!(
                            "MISMATCH {}: {}x{}x{} zen={:.16}.. refs={:?}",
                            entry.path,
                            result.width,
                            result.height,
                            result.channels,
                            &pixel_hash,
                            ref_names,
                        );
                    }
                }
            }
            Err(e) => {
                decode_err += 1;
                if decode_err <= 10 {
                    eprintln!("DECODE_ERR {}: {e}", entry.path);
                }
            }
        }

        if total % 200 == 0 {
            eprint!("\r  [{total}/{} files]", manifest.files.len());
        }
    }
    eprintln!();

    println!("═══ Results ═══");
    println!("  Total:         {total}");
    println!("  Decoded OK:    {decoded_ok}");
    println!("  Decode errors: {decode_err}");
    println!("  Skipped:       {skipped}");
    println!("  Hash match:    {hash_match}");
    println!("  Hash mismatch: {hash_mismatch}");
    println!("  No reference:  {no_reference}");
    println!();
    println!("Per format:");
    for (fmt, (tot, ok, matched, mismatched)) in &format_stats {
        let err = tot - ok;
        println!(
            "  {fmt:6}: {tot:5} total, {ok:5} decoded, {matched:5} matched, {mismatched:5} mismatched, {err:5} errors"
        );
    }

    if hash_mismatch > 0 || decode_err > 0 {
        std::process::exit(1);
    }
}
