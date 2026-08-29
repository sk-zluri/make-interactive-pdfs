"""Regression coverage for the safe verified-links replay flow."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from tests.regression_test import make_piecewise


REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "run_isolated.py"
sys.path.insert(0, str(REPO / "scripts"))
import make_interactive_pdf as linker  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *arguments],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


class VerifiedLinksReplayTests(unittest.TestCase):
    def test_verified_subset_fails_closed_when_required_ocr_did_not_finish(self) -> None:
        high_internal = linker.AddedLink(
            kind="internal",
            source_page=0,
            rect=(10.0, 10.0, 40.0, 30.0),
            target_page=1,
            matched_by="pagination-segment",
            confidence="high",
        )

        self.assertEqual(
            linker.verified_link_subset(
                [high_internal],
                relevant_ocr_failures=[1],
            ),
            [],
        )
        self.assertEqual(
            linker.verified_link_subset(
                [high_internal],
                navigation_ocr_failures=[1],
            ),
            [],
        )

    def test_verified_subset_excludes_unconfirmed_candidates(self) -> None:
        high_internal = linker.AddedLink(
            kind="internal",
            source_page=0,
            rect=(10.0, 10.0, 40.0, 30.0),
            target_page=1,
            matched_by="pagination-segment",
            confidence="high",
        )
        medium_internal = linker.AddedLink(
            kind="internal",
            source_page=0,
            rect=(50.0, 10.0, 80.0, 30.0),
            target_page=2,
            matched_by="pagination-segment",
            confidence="medium",
        )
        visible_url = linker.AddedLink(
            kind="external",
            source_page=0,
            rect=(90.0, 10.0, 140.0, 30.0),
            uri="https://example.com",
            label="https://example.com",
        )
        recovered_url = linker.AddedLink(
            kind="external",
            source_page=0,
            rect=(150.0, 10.0, 200.0, 30.0),
            uri="https://example.com",
            label="recovered annotation metadata",
        )

        subset = linker.verified_link_subset(
            [high_internal, medium_internal, visible_url, recovered_url]
        )

        self.assertEqual(subset, [high_internal, visible_url])

    def test_confirmed_replay_reads_only_the_integrity_checked_subset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verified-subset-test-") as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            source.write_bytes(b"not parsed by the manifest loader")
            high = linker.AddedLink(
                kind="internal",
                source_page=0,
                rect=(10.0, 10.0, 40.0, 30.0),
                target_page=1,
                matched_by="pagination-segment",
                confidence="high",
            )
            medium = linker.AddedLink(
                kind="internal",
                source_page=0,
                rect=(50.0, 10.0, 80.0, 30.0),
                target_page=2,
                matched_by="pagination-segment",
                confidence="medium",
            )
            subset = [asdict(high)]
            report = {
                "schema_version": 2,
                "status": "NEEDS_REVIEW",
                "input_sha256": sha256(source),
                "links": [asdict(high), asdict(medium)],
                "verified_links_offer": {
                    "eligible": True,
                    "selection_policy": linker.VERIFIED_LINKS_SELECTION_POLICY,
                    "links": subset,
                    "subset_sha256": linker.canonical_json_sha256(subset),
                },
            }
            report_path = root / "review.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            links, _ = linker.load_link_manifest(
                report_path,
                source,
                3,
                allow_legacy=False,
                allow_confirmed_links=True,
            )
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].target_page, 1)

            report["verified_links_offer"]["links"][0]["target_page"] = 0
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity"):
                linker.load_link_manifest(
                    report_path,
                    source,
                    3,
                    allow_legacy=False,
                    allow_confirmed_links=True,
                )

    def test_only_link_relevant_ocr_failures_block_release(self) -> None:
        planned = [
            linker.AddedLink(
                kind="internal",
                source_page=0,
                rect=(10.0, 10.0, 40.0, 30.0),
                target_page=4,
            )
        ]
        relevant, unrelated = linker.classify_ocr_failure_relevance(
            [1, 2, 3, 5, 7, 11],
            toc_pages={0},
            # The production analyzer keeps this ordered while it batches OCR.
            navigation_ocr_pages=[1],
            navigation_candidate_pages=[],
            planned_links=planned,
            unresolved_rows=[
                {
                    "source_page": 3,
                    "evidence": {"candidate_target_pages": [7]},
                }
            ],
            margin_checks=[],
        )
        self.assertEqual(relevant, [1, 2, 3, 5, 7])
        self.assertEqual(unrelated, [11])

    def test_failed_adjacent_contents_candidate_stays_relevant(self) -> None:
        relevant, unrelated = linker.classify_ocr_failure_relevance(
            [2, 3, 18],
            toc_pages={1},
            navigation_ocr_pages=[],
            # Page index 2 is the failed continuation beside a detected TOC.
            navigation_candidate_pages=[2],
            planned_links=[],
            unresolved_rows=[],
            margin_checks=[],
        )

        self.assertEqual(relevant, [2, 3])
        self.assertEqual(unrelated, [18])

    def test_review_can_replay_only_confirmed_links(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verified-links-test-") as temporary:
            root = Path(temporary)
            source = root / "piecewise-missing.pdf"
            review_output = root / "strict-output.pdf"
            review_report = root / "strict-review.json"
            final_output = root / "confirmed-only.pdf"
            final_report = root / "confirmed-only-report.json"
            verification = root / "verification.json"

            make_piecewise(source, include_missing=True)
            original_hash = sha256(source)

            strict = run(
                "make",
                str(source),
                "--output",
                str(review_output),
                "--report-json",
                str(review_report),
            )
            self.assertEqual(strict.returncode, 2, strict.stdout + strict.stderr)
            self.assertFalse(review_output.exists())
            review = json.loads(review_report.read_text(encoding="utf-8"))
            offer = review.get("verified_links_offer")
            self.assertIsInstance(offer, dict)
            self.assertTrue(offer.get("eligible"), review)
            self.assertGreater(sum(offer.get("safe_link_counts", {}).values()), 0)
            self.assertGreater(offer.get("unresolved_toc_rows", 0), 0)
            self.assertEqual(sha256(source), original_hash)

            replay = run(
                "make",
                str(source),
                "--link-manifest",
                str(review_report),
                "--publish-confirmed-links",
                "--output",
                str(final_output),
                "--report-json",
                str(final_report),
            )
            self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
            self.assertTrue(final_output.is_file())
            final = json.loads(final_report.read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "PASS")
            self.assertEqual(final["mode"], "verified-links-only")
            self.assertEqual(final["publication"]["mode"], "verified-links-only")
            self.assertEqual(final["publication"]["strict_result"], "NEEDS_REVIEW")
            self.assertEqual(
                final["publication"]["omitted_toc_rows"],
                offer["unresolved_toc_rows"],
            )
            self.assertEqual(
                final["publication"]["suspected_unparsed_toc_rows"],
                offer["suspected_unparsed_toc_rows"],
            )
            self.assertEqual(
                final["publication"]["selection_policy"],
                linker.VERIFIED_LINKS_SELECTION_POLICY,
            )
            self.assertEqual(
                final["publication"]["confirmed_links_replayed"],
                len(offer["links"]),
            )
            self.assertEqual(sha256(source), original_hash)

            checked = run(
                "verify",
                str(source),
                str(final_output),
                "--link-report",
                str(final_report),
                "--json",
                str(verification),
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertEqual(
                json.loads(verification.read_text(encoding="utf-8"))["status"], "PASS"
            )

    def test_replay_requires_an_engine_offered_review_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verified-links-reject-") as temporary:
            root = Path(temporary)
            source = root / "piecewise-missing.pdf"
            report_path = root / "review.json"
            rejected_output = root / "must-not-exist.pdf"
            rejected_report = root / "must-not-exist-report.json"

            make_piecewise(source, include_missing=True)
            strict = run(
                "make",
                str(source),
                "--output",
                str(root / "strict-output.pdf"),
                "--report-json",
                str(report_path),
            )
            self.assertEqual(strict.returncode, 2, strict.stdout + strict.stderr)
            review = json.loads(report_path.read_text(encoding="utf-8"))
            review["verified_links_offer"]["eligible"] = False
            report_path.write_text(json.dumps(review), encoding="utf-8")

            replay = run(
                "make",
                str(source),
                "--link-manifest",
                str(report_path),
                "--publish-confirmed-links",
                "--output",
                str(rejected_output),
                "--report-json",
                str(rejected_report),
            )
            self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
            self.assertIn("eligible NEEDS_REVIEW", replay.stderr)
            self.assertFalse(rejected_output.exists())
            self.assertFalse(rejected_report.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
