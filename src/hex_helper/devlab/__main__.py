"""CLI entrypoint for HexHelper DevLab offline replay, evolution report, version compare, and doctor.

Usage:
    python -m hex_helper.devlab list [--dir PATH] [--limit N] [--json]
    python -m hex_helper.devlab replay <match_seq|latest> [--dir PATH] [--out DIR] [--no-write]
    python -m hex_helper.devlab report [--last N] [--since YYYY-MM-DD] [--out DIR] [--dir PATH] [--json]
    python -m hex_helper.devlab compare <ver_a> <ver_b> [--out DIR] [--dir PATH] [--json]
    python -m hex_helper.devlab doctor [--dir PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from hex_helper.devlab.analyzer import DevLabAnalyzer


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hex_helper.devlab",
        description="HexHelper DevLab Flight Recorder offline replay, evolution report, and diagnostics CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List recorded DevLab matches.")
    p_list.add_argument("--dir", dest="root_dir", default=None, help="Override DevLab root directory.")
    p_list.add_argument("--limit", type=int, default=0, help="Show only the last N matches.")
    p_list.add_argument("--json", dest="as_json", action="store_true", help="Output JSON.")

    # replay
    p_replay = sub.add_parser("replay", help="Replay a single match and generate timeline markdown.")
    p_replay.add_argument("target", help="Match seq (e.g. 42), 'latest', dir name, or direct path.")
    p_replay.add_argument("--dir", dest="root_dir", default=None, help="Override DevLab root directory.")
    p_replay.add_argument("--out", dest="out_dir", default=None, help="Override timeline output directory.")
    p_replay.add_argument("--no-write", action="store_true", help="Do not write timeline or replay_summary.json.")
    p_replay.add_argument("--json", dest="as_json", action="store_true", help="Output replay summary JSON.")

    # report
    p_report = sub.add_parser("report", help="Generate multi-match 5-pillar evolution report & backlog JSON.")
    p_report.add_argument("--last", type=int, default=20, help="Number of recent matches to include (default: 20).")
    p_report.add_argument("--since", default=None, help="Filter matches started_at >= YYYY-MM-DD.")
    p_report.add_argument("--out", dest="out_dir", default=None, help="Override report output directory.")
    p_report.add_argument("--dir", dest="root_dir", default=None, help="Override DevLab root directory.")
    p_report.add_argument("--json", dest="as_json", action="store_true", help="Output evolution backlog JSON.")

    # compare
    p_cmp = sub.add_parser("compare", help="Compare two app versions across all 5 evolution pillars.")
    p_cmp.add_argument("ver_a", help="Baseline version prefix (e.g. 1.4.7-old).")
    p_cmp.add_argument("ver_b", help="Candidate version prefix (e.g. 1.4.8-new).")
    p_cmp.add_argument("--out", dest="out_dir", default=None, help="Override diff report output directory.")
    p_cmp.add_argument("--dir", dest="root_dir", default=None, help="Override DevLab root directory.")
    p_cmp.add_argument("--json", dest="as_json", action="store_true", help="Output comparison JSON.")

    # doctor
    p_doc = sub.add_parser("doctor", help="Audit DevLab directory health, quotas, PII zero-leak scan, and drops.")
    p_doc.add_argument("--dir", dest="root_dir", default=None, help="Override DevLab root directory.")
    p_doc.add_argument("--json", dest="as_json", action="store_true", help="Output JSON.")

    return parser


def _cmd_list(analyzer: DevLabAnalyzer, args: argparse.Namespace) -> int:
    matches = analyzer.list_matches()
    if args.limit and args.limit > 0:
        matches = matches[-args.limit :]

    if args.as_json:
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return 0

    if not matches:
        print(f"No recorded DevLab matches found in {analyzer.root_dir}")
        return 0

    print(f"DevLab Matches ({len(matches)}) — {analyzer.root_dir}")
    print(f"{'Seq':>6}  {'Started (UTC)':<20}  {'Hero':<14}  {'Version':<16}  {'Mode':<8}  {'Res':<4}  {'Dur':>5}  {'Evts':>5}  {'ROI':>3}")
    print("-" * 98)
    for m in matches:
        seq = int(m.get("seq") or 0)
        started = str(m.get("started_at") or "")[:19]
        hero = str(m.get("hero_id") or m.get("hero_name") or "-")[:14]
        ver = str(m.get("app_version") or "-")[:16]
        mode = "NON-MAY" if m.get("is_non_mayhem") else "MAYHEM"
        won = int(m.get("won", -1))
        res = {1: "WIN", 0: "LOSS"}.get(won, "UNK")
        dur = f"{int(m.get('duration_s') or 0)}s"
        evts = int(m.get("event_count") or 0)
        rois = int(m.get("roi_count") or 0)
        print(f"#{seq:05d}  {started:<20}  {hero:<14}  {ver:<16}  {mode:<8}  {res:<4}  {dur:>5}  {evts:>5}  {rois:>3}")
    return 0


def _cmd_replay(analyzer: DevLabAnalyzer, args: argparse.Namespace) -> int:
    mdir = analyzer.resolve_match_dir(args.target)
    if mdir is None:
        print(f"[ERROR] Match not found: {args.target} (root={analyzer.root_dir})", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else None
    result = analyzer.replay_match(mdir, write_outputs=not args.no_write, out_dir=out_dir)

    if args.as_json:
        print(json.dumps(result.to_summary_dict(), ensure_ascii=False, indent=2))
        return 0

    res_lbl = {1: "WIN", 0: "LOSS"}.get(result.won, "UNKNOWN")
    mode_lbl = "Non-Mayhem" if result.is_non_mayhem else "ARAM Mayhem"
    print(f"Match #{result.seq:05d} | {result.hero_name or result.hero_id or 'unknown'} | {mode_lbl} | {res_lbl} ({result.duration_s}s)")
    print(f"  Version: {result.app_version or '-'} | EOG: {result.eog_source or '-'} | Events: {result.event_count} (corrupt={result.corrupt_lines}) | ROIs: {len(result.roi_paths)}")
    p1 = result.kpis.get("recommendation", {})
    p4 = result.kpis.get("coach", {})
    p5 = result.kpis.get("runtime", {})
    print(
        f"  KPIs: aug_top1={_fmt_pct(p1.get('augment_top1_adoption_rate'))} "
        f"item_top4={_fmt_pct(p1.get('item_top4_adoption_rate'))} "
        f"coach_turns={p4.get('coach_turns', 0)} "
        f"tick_p95={p5.get('tick_vision_p95_ms', 0)}ms "
        f"stalls>=50ms={p5.get('main_thread_ge_50ms_stalls', 0)}"
    )
    if result.issues:
        print(f"  Issues triggered ({len(result.issues)}):")
        for iss in result.issues:
            print(f"    - [{iss.severity}] {iss.detector_rule}: {iss.summary}")
    else:
        print("  Issues triggered: 0")
    if result.timeline_path:
        print(f"  Timeline: {result.timeline_path}")
    if result.summary_path:
        print(f"  Summary : {result.summary_path}")
    return 0


def _cmd_report(analyzer: DevLabAnalyzer, args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else None
    rep = analyzer.generate_evolution_report(
        last_n=args.last,
        since=args.since,
        out_dir=out_dir,
    )

    if args.as_json:
        print(json.dumps(rep.to_backlog_dict(), ensure_ascii=False, indent=2))
        return 0

    p1 = rep.pillars.get("recommendation", {})
    p3 = rep.pillars.get("ux", {})
    p4 = rep.pillars.get("coach", {})
    p5 = rep.pillars.get("runtime", {})
    print(
        f"DevLab Evolution Report ({rep.stamp}) | Matches: {rep.total_matches} "
        f"(Mayhem={rep.mayhem_matches}, Isolated={rep.non_mayhem_matches})"
    )
    print(
        f"  P1 Rec    : winrate={_fmt_pct(p1.get('mayhem_winrate'))}, "
        f"aug_top1={_fmt_pct(p1.get('augment_top1_adoption_rate'))}, "
        f"item_top4={_fmt_pct(p1.get('item_top4_adoption_rate'))}, "
        f"overrides={p1.get('plan_override_count', 0)}"
    )
    print(
        f"  P3 UX     : rapid_dismiss={_fmt_pct(p3.get('rapid_dismiss_rate'))}, "
        f"scroll_overflow={_fmt_pct(p3.get('scroll_overflow_rate'))}, "
        f"ghost_repopup={p3.get('ghost_repopup_count', 0)}"
    )
    print(
        f"  P4 Coach  : turns={p4.get('coach_turns', 0)}, "
        f"picker_timely={_fmt_pct(p4.get('picker_ai_timely_rate'))}, "
        f"death_timely={_fmt_pct(p4.get('death_ai_timely_rate'))}, "
        f"grounding_blk={_fmt_pct(p4.get('grounding_block_rate'))}"
    )
    print(
        f"  P5 Runtime: eog_complete={_fmt_pct(p5.get('eog_complete_rate'))}, "
        f"tick_p95={p5.get('tick_vision_p95_ms', 0)}ms, "
        f"stalls>=50ms={p5.get('main_thread_ge_50ms_stalls', 0)}, "
        f"click_gap={p5.get('click_timer_max_gap_ms', 0)}ms"
    )
    print(f"  Detected issues ({len(rep.issues)}):")
    for iss in rep.issues:
        print(f"    - [{iss.severity}] {iss.detector_rule} (n={iss.sample_count}): {iss.summary}")
    print(f"  Report : {rep.report_path}")
    print(f"  Backlog: {rep.backlog_path}")
    return 0


def _cmd_compare(analyzer: DevLabAnalyzer, args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else None
    diff = analyzer.compare_versions(args.ver_a, args.ver_b, out_dir=out_dir)

    if args.as_json:
        print(json.dumps(diff.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(
        f"DevLab Version Compare: {diff.ver_a} ({diff.matches_a} matches, {diff.mayhem_matches_a} mayhem) "
        f"vs {diff.ver_b} ({diff.matches_b} matches, {diff.mayhem_matches_b} mayhem)"
    )
    key_highlights = [
        ("recommendation", "mayhem_winrate"),
        ("recommendation", "augment_top1_adoption_rate"),
        ("recommendation", "item_top4_adoption_rate"),
        ("ux", "rapid_dismiss_rate"),
        ("coach", "grounding_block_rate"),
        ("runtime", "eog_complete_rate"),
        ("runtime", "tick_vision_p95_ms"),
    ]
    for pillar, key in key_highlights:
        row = diff.deltas.get(pillar, {}).get(key)
        if row is None:
            continue
        print(f"  {pillar}.{key}: {row['a']} -> {row['b']} (delta={row['delta']}, {row['status']})")
    print(f"  Issues: {len(diff.issues_a)} -> {len(diff.issues_b)}")
    print(f"  Diff report: {diff.diff_path}")
    return 0


def _cmd_doctor(analyzer: DevLabAnalyzer, args: argparse.Namespace) -> int:
    diag: dict[str, Any] = analyzer.doctor()

    if args.as_json:
        print(json.dumps(diag, ensure_ascii=False, indent=2))
        return 1 if diag.get("status") == "fail" else 0

    overall = str(diag.get("status") or "unknown").upper()
    print(f"DevLab Doctor [{overall}] — {diag.get('root_dir')}")
    for name, info in (diag.get("checks") or {}).items():
        st = str(info.get("status") or "unknown").upper()
        msg = str(info.get("message") or "")
        print(f"  [{st:<4}] {name:<18} {msg}")
        if name == "pii_scan" and info.get("findings"):
            for f in info["findings"][:5]:
                print(f"         leak: {f.get('file')}:{f.get('line')} ({f.get('pattern')})")
    return 1 if diag.get("status") == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_console()
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    root_dir = Path(args.root_dir) if getattr(args, "root_dir", None) else None
    analyzer = DevLabAnalyzer(root_dir=root_dir)

    if args.command == "list":
        return _cmd_list(analyzer, args)
    if args.command == "replay":
        return _cmd_replay(analyzer, args)
    if args.command == "report":
        return _cmd_report(analyzer, args)
    if args.command == "compare":
        return _cmd_compare(analyzer, args)
    if args.command == "doctor":
        return _cmd_doctor(analyzer, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
