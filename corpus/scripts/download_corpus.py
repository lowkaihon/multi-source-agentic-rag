#!/usr/bin/env python3
"""
MAS Regulatory Compliance Corpus — PDF Download Script
=====================================================
Phase 1, Step 1 of Project 2 Implementation.

Downloads all MAS regulatory PDFs from mas.gov.sg and structured data
from OpenSanctions. Produces a download manifest with status tracking.

Usage:
    python download_corpus.py [--output-dir ./pdfs] [--manifest ./manifests/corpus_manifest.json]

Prerequisites:
    pip install requests tqdm

Network requirements:
    - Access to mas.gov.sg (PDF downloads)
    - Access to data.opensanctions.org (structured data)
"""

import json
import os
import sys
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CORPUS_DIR = SCRIPT_DIR.parent
DEFAULT_OUTPUT_DIR = CORPUS_DIR / "pdfs"
DEFAULT_MANIFEST = CORPUS_DIR / "manifests" / "corpus_manifest.json"
DOWNLOAD_LOG = CORPUS_DIR / "manifests" / "download_log.json"

REQUEST_TIMEOUT = 60  # seconds
DELAY_BETWEEN_DOWNLOADS = 2  # seconds — be polite to MAS servers
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("corpus-dl")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    doc_id: str
    title: str
    url: str
    filename: str
    category: str
    status: str  # "success", "failed", "skipped"
    file_size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session() -> requests.Session:
    """Create a requests session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def sha256_file(filepath: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    desc: str = "",
) -> tuple[bool, Optional[str]]:
    """
    Download a file with progress display.
    Returns (success: bool, error_message: Optional[str]).
    """
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        total_size = int(response.headers.get("Content-Length", 0))

        # Verify we got a PDF (or acceptable content)
        if "html" in content_type.lower() and total_size < 5000:
            return False, f"Got HTML instead of PDF (likely a landing page). Content-Type: {content_type}"

        dest.parent.mkdir(parents=True, exist_ok=True)

        if HAS_TQDM and total_size > 0:
            with open(dest, "wb") as f, tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                desc=desc[:40],
                leave=False,
            ) as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    pbar.update(len(chunk))
        else:
            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

        # Basic validation
        file_size = dest.stat().st_size
        if file_size < 1024:  # Less than 1KB is suspicious for a PDF
            return False, f"Downloaded file too small ({file_size} bytes) — likely not a valid PDF"

        return True, None

    except requests.exceptions.HTTPError as e:
        return False, f"HTTP {e.response.status_code}: {str(e)}"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error: {str(e)}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

def load_manifest(manifest_path: Path) -> dict:
    """Load the corpus manifest JSON."""
    with open(manifest_path, "r") as f:
        return json.load(f)


def collect_documents(manifest: dict) -> list[dict]:
    """Flatten all document categories into a single list."""
    docs = []
    doc_categories = ["core_guidelines", "consultation_papers", "enforcement_reports", "info_papers_circulars"]

    for category in doc_categories:
        if category in manifest.get("documents", {}):
            for doc in manifest["documents"][category]:
                docs.append(doc)

    return docs


def download_pdfs(
    manifest_path: Path = DEFAULT_MANIFEST,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    force: bool = False,
) -> list[DownloadResult]:
    """Download all PDFs listed in the corpus manifest."""

    manifest = load_manifest(manifest_path)
    documents = collect_documents(manifest)
    session = get_session()
    results: list[DownloadResult] = []

    log.info(f"Corpus: {manifest['corpus_name']}")
    log.info(f"Documents to download: {len(documents)}")
    log.info(f"Output directory: {output_dir}")
    log.info("=" * 60)

    for i, doc in enumerate(documents, 1):
        doc_id = doc["id"]
        title = doc.get("short_name", doc["title"])
        category = doc["category"]
        filename = doc.get("filename", f"{doc_id}.pdf")
        url = doc.get("url", "")

        # Determine destination path
        dest = output_dir / category / filename

        log.info(f"[{i}/{len(documents)}] {title}")

        # Skip if already downloaded (unless --force)
        if dest.exists() and not force:
            file_size = dest.stat().st_size
            if file_size > 1024:
                log.info(f"  → Already exists ({file_size:,} bytes), skipping")
                results.append(DownloadResult(
                    doc_id=doc_id, title=title, url=url, filename=filename,
                    category=category, status="skipped",
                    file_size_bytes=file_size,
                    sha256=sha256_file(dest),
                    timestamp=datetime.now().isoformat(),
                ))
                continue

        if not url:
            log.warning(f"  → No URL provided, skipping")
            results.append(DownloadResult(
                doc_id=doc_id, title=title, url="", filename=filename,
                category=category, status="failed",
                error="No URL in manifest",
                timestamp=datetime.now().isoformat(),
            ))
            continue

        # Skip URLs that are landing pages (not direct PDF links)
        if not url.endswith(".pdf") and "pdf_url_pattern" not in doc:
            log.warning(f"  → URL is a landing page, not a direct PDF link: {url}")
            log.warning(f"    Manual download required. Add direct PDF URL to manifest.")
            results.append(DownloadResult(
                doc_id=doc_id, title=title, url=url, filename=filename,
                category=category, status="failed",
                error="URL is a landing page, not a direct PDF. Requires manual URL extraction.",
                timestamp=datetime.now().isoformat(),
            ))
            continue

        # Try primary URL
        log.info(f"  → Downloading from: {url[:80]}...")
        success, error = download_file(session, url, dest, desc=title)

        # Try alternate URL if primary failed
        if not success and "alt_url" in doc:
            alt_url = doc["alt_url"]
            log.info(f"  → Primary failed ({error}), trying alt URL...")
            success, error = download_file(session, alt_url, dest, desc=title)
            if success:
                url = alt_url

        if success:
            file_size = dest.stat().st_size
            file_hash = sha256_file(dest)
            log.info(f"  OK Downloaded ({file_size:,} bytes)")
            results.append(DownloadResult(
                doc_id=doc_id, title=title, url=url, filename=filename,
                category=category, status="success",
                file_size_bytes=file_size,
                sha256=file_hash,
                timestamp=datetime.now().isoformat(),
            ))
        else:
            log.error(f"  ✗ Failed: {error}")
            results.append(DownloadResult(
                doc_id=doc_id, title=title, url=url, filename=filename,
                category=category, status="failed",
                error=error,
                timestamp=datetime.now().isoformat(),
            ))

        # Polite delay
        time.sleep(DELAY_BETWEEN_DOWNLOADS)

    return results


def download_opensanctions(output_dir: Path = DEFAULT_OUTPUT_DIR) -> list[DownloadResult]:
    """Download OpenSanctions MAS enforcement data."""
    session = get_session()
    results = []
    data_dir = output_dir.parent / "data" / "opensanctions"
    data_dir.mkdir(parents=True, exist_ok=True)

    sources = [
        {
            "id": "SD-001-csv",
            "title": "OpenSanctions MAS Enforcement (CSV)",
            "url": "https://data.opensanctions.org/datasets/latest/sg_mas_enforcement_actions/targets.simple.csv",
            "filename": "sg_mas_enforcement_actions.csv",
        },
        {
            "id": "SD-001-json",
            "title": "OpenSanctions MAS Enforcement (JSON)",
            "url": "https://data.opensanctions.org/datasets/latest/sg_mas_enforcement_actions/entities.ftm.json",
            "filename": "sg_mas_enforcement_actions.json",
        },
    ]

    log.info("")
    log.info("=" * 60)
    log.info("Downloading OpenSanctions structured data...")
    log.info("=" * 60)

    for source in sources:
        dest = data_dir / source["filename"]
        log.info(f"  → {source['title']}")
        success, error = download_file(session, source["url"], dest, desc=source["title"])

        if success:
            file_size = dest.stat().st_size
            log.info(f"  OK Downloaded ({file_size:,} bytes)")
            results.append(DownloadResult(
                doc_id=source["id"], title=source["title"],
                url=source["url"], filename=source["filename"],
                category="structured_data", status="success",
                file_size_bytes=file_size,
                sha256=sha256_file(dest),
                timestamp=datetime.now().isoformat(),
            ))
        else:
            log.error(f"  ✗ Failed: {error}")
            results.append(DownloadResult(
                doc_id=source["id"], title=source["title"],
                url=source["url"], filename=source["filename"],
                category="structured_data", status="failed",
                error=error,
                timestamp=datetime.now().isoformat(),
            ))

        time.sleep(1)

    return results


def save_download_log(results: list[DownloadResult], log_path: Path = DOWNLOAD_LOG):
    """Save download results to a JSON log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_data = {
        "download_timestamp": datetime.now().isoformat(),
        "total_documents": len(results),
        "successful": sum(1 for r in results if r.status == "success"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "results": [asdict(r) for r in results],
    }
    with open(log_path, "w") as f:
        json.dump(log_data, f, indent=2)
    log.info(f"\nDownload log saved to: {log_path}")


def print_summary(results: list[DownloadResult]):
    """Print download summary."""
    success = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]

    total_bytes = sum(r.file_size_bytes or 0 for r in results if r.file_size_bytes)

    print("\n" + "=" * 60)
    print("DOWNLOAD SUMMARY")
    print("=" * 60)
    print(f"  Total documents:  {len(results)}")
    print(f"  Successful:       {len(success)}")
    print(f"  Failed:           {len(failed)}")
    print(f"  Skipped:          {len(skipped)}")
    print(f"  Total size:       {total_bytes / (1024*1024):.1f} MB")

    if failed:
        print("\n  FAILED DOWNLOADS:")
        for r in failed:
            print(f"    [{r.doc_id}] {r.title}")
            print(f"      Error: {r.error}")
            if r.url:
                print(f"      URL: {r.url}")

    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download MAS regulatory corpus PDFs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Output directory for PDFs")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="Path to corpus manifest JSON")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if files exist")
    parser.add_argument("--skip-opensanctions", action="store_true",
                        help="Skip OpenSanctions data download")
    parser.add_argument("--pdfs-only", action="store_true",
                        help="Only download PDFs, skip structured data")
    args = parser.parse_args()

    # Download PDFs
    results = download_pdfs(
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        force=args.force,
    )

    # Download OpenSanctions
    if not args.pdfs_only and not args.skip_opensanctions:
        os_results = download_opensanctions(output_dir=args.output_dir)
        results.extend(os_results)

    # Save log and print summary
    save_download_log(results)
    print_summary(results)


if __name__ == "__main__":
    main()
