"""Terminal entrypoint for the CDR Report App."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.domain.provider_models import SearchSubject
from cdr_report_app.integrations.http import HttpClient
from cdr_report_app.integrations.providers import CroPdfAttachmentProvider, PsrmsFirAttachmentProvider
from cdr_report_app.integrations.providers import UnifiedLookupService
from cdr_report_app.logging_config import setup_logging
from cdr_report_app.services.analysis_service import build_report_analysis
from cdr_report_app.services.ingestion_service import ingest_cdr_file
from cdr_report_app.services.report_service import generate_report
from cdr_report_app.settings import load_settings, provider_readiness

logger = logging.getLogger(__name__)


def _safe_json_print(payload: object) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="backslashreplace").decode("ascii"))


def _crime_context_from_settings(settings) -> CrimeContext:
    crime = settings.crime
    return CrimeContext(
        fir_no=crime.fir_no,
        police_station=crime.police_station,
        sections_of_law=crime.sections_of_law,
        crime_date=crime.crime_date,
        crime_time=crime.crime_time,
        crime_place=crime.crime_place,
        crime_lat=crime.crime_lat,
        crime_lng=crime.crime_lng,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdr-report",
        description="Terminal-first CDR report generation toolkit.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/default.yaml"),
        help="Path to YAML config file.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to CDR input file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PDF path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without generating a report.",
    )
    parser.add_argument(
        "--inspect-input",
        action="store_true",
        help="Read and normalize the input file, then print ingestion summary.",
    )
    parser.add_argument(
        "--inspect-analysis",
        action="store_true",
        help="Build section analysis from the input file and print summary counts.",
    )
    parser.add_argument(
        "--render-pdf",
        action="store_true",
        help="Generate a PDF report from the input file.",
    )
    parser.add_argument(
        "--provider-status",
        action="store_true",
        help="Show which provider integrations are configured and ready.",
    )
    parser.add_argument(
        "--test-provider",
        type=str,
        help="Run a live test against a single provider using the supplied subject fields.",
    )
    parser.add_argument(
        "--mobile",
        type=str,
        help="Mobile/MSISDN to use for provider tests.",
    )
    parser.add_argument(
        "--cnic",
        type=str,
        help="CNIC to use for provider tests.",
    )
    parser.add_argument(
        "--imei",
        type=str,
        help="IMEI to use for provider tests.",
    )
    parser.add_argument(
        "--lat",
        type=float,
        help="Latitude to use for provider tests such as nearest police station.",
    )
    parser.add_argument(
        "--lng",
        type=float,
        help="Longitude to use for provider tests such as nearest police station.",
    )
    parser.add_argument(
        "--test-cro-report",
        action="store_true",
        help="Fetch a CRO PDF report attachment by CRO number.",
    )
    parser.add_argument(
        "--cro-no",
        type=str,
        help="CRO number for CRO report attachment tests.",
    )
    parser.add_argument(
        "--test-fir-report",
        action="store_true",
        help="Fetch a PSRMS FIR report attachment by FIR number, year, and PS ID.",
    )
    parser.add_argument(
        "--fir-no",
        type=str,
        help="FIR number for FIR report tests.",
    )
    parser.add_argument(
        "--fir-year",
        type=str,
        help="FIR year for FIR report tests.",
    )
    parser.add_argument(
        "--ps-id",
        type=str,
        help="Police station ID for FIR report tests.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings(config_path=args.config, output_override=args.output)
    log_path = setup_logging(settings.app.log_level, settings.app.log_dir, settings.app.log_to_file)

    print("CDR Report App settings loaded.")
    print(f"Config: {settings.config_path}")
    print(f"Environment: {settings.app.environment}")
    print(f"Output directory: {settings.app.output_dir}")
    print(f"Log level: {settings.app.log_level}")
    if log_path:
        print(f"Log file: {log_path}")
    if settings.env_file:
        print(f"Env file: {settings.env_file}")
    else:
        print("Env file: not found")
    if args.input:
        print(f"Input: {args.input}")
    if args.output:
        print(f"Output: {args.output}")
    if args.dry_run:
        print("Dry run requested.")

    logger.info(
        "CLI started | inspect_input=%s inspect_analysis=%s render_pdf=%s provider_status=%s test_provider=%s input=%s output=%s",
        args.inspect_input,
        args.inspect_analysis,
        args.render_pdf,
        args.provider_status,
        args.test_provider,
        args.input,
        args.output,
    )

    if args.inspect_input:
        if not args.input:
            raise SystemExit("--inspect-input requires --input")
        result = ingest_cdr_file(args.input)
        print("Ingestion summary:")
        print(f" - operator: {result.source_operator or 'unknown'}")
        print(f" - rows: {result.row_count}")
        print(f" - metadata.name: {result.metadata.name}")
        print(f" - metadata.msisdn: {result.metadata.msisdn}")
        print(f" - metadata.cnic: {result.metadata.cnic}")
        print(f" - normalized columns: {len(result.column_mappings)}")
        if result.warnings:
            print("Warnings:")
            for warning in result.warnings:
                print(f" - {warning.code}: {warning.message}")

    if args.inspect_analysis:
        if not args.input:
            raise SystemExit("--inspect-analysis requires --input")
        ingestion = ingest_cdr_file(args.input)
        analysis = build_report_analysis(
            ingestion,
            settings,
            crime_context=_crime_context_from_settings(settings),
            db_results=None,
        )
        print("Analysis summary:")
        print(f" - total_records: {analysis.quick_stats.total_records}")
        print(f" - unique_numbers: {analysis.quick_stats.unique_numbers}")
        print(f" - repeated_contacts: {analysis.quick_stats.repeated_contacts}")
        print(f" - top_locations: {len(analysis.top_locations)}")
        print(f" - long_stays: {len(analysis.long_stays)}")
        print(f" - movement_steps: {len(analysis.movement_steps)}")
        print(f" - bursts: {len(analysis.bursts)}")
        print(f" - short_codes: {len(analysis.short_codes)}")
        print(f" - imei_records: {len(analysis.imei_records)}")
        print(f" - imsi_records: {len(analysis.imsi_records)}")
        print(f" - top_contacts: {len(analysis.top_contacts)}")

    if args.render_pdf:
        if not args.input:
            raise SystemExit("--render-pdf requires --input")
        output_path = args.output or settings.app.output_dir / f"{args.input.stem}_report.pdf"
        logger.info("Rendering PDF | input=%s output=%s", args.input, output_path)
        generate_report(
            settings=settings,
            input_path=args.input,
            output_path=output_path,
            crime_context=_crime_context_from_settings(settings),
            db_results=None,
        )
        print(f"PDF generated: {output_path}")

    if args.provider_status:
        rows = provider_readiness(settings)
        print("Provider readiness:")
        for row in rows:
            status = "READY" if row["ready"] else "NOT READY"
            enabled = "enabled" if row["enabled"] else "disabled"
            print(f" - {row['provider']}: {status} ({enabled})")
            if row["missing"]:
                print(f"   missing: {', '.join(row['missing'])}")

    if args.test_provider:
        subject = SearchSubject(
            cnic=args.cnic,
            mobile=args.mobile,
            imei=args.imei,
            latitude=args.lat,
            longitude=args.lng,
        )
        service = UnifiedLookupService(settings)
        logger.info("Testing provider | provider=%s mobile=%s cnic=%s imei=%s lat=%s lng=%s", args.test_provider, args.mobile, args.cnic, args.imei, args.lat, args.lng)
        result = service.lookup_one(args.test_provider, subject)
        print(f"Provider test: {args.test_provider}")
        print(f" - status: {result.status}")
        print(f" - hit: {result.hit}")
        print(f" - summary: {result.summary}")
        if result.errors:
            print(" - errors:")
            for error in result.errors:
                print(f"   - {error}")
        if result.data is not None:
            print(" - normalized data:")
            _safe_json_print(result.data.model_dump(mode="json"))
        if result.raw is not None:
            print(" - raw response:")
            _safe_json_print(result.raw)

    if args.test_cro_report:
        if not args.cro_no:
            raise SystemExit("--test-cro-report requires --cro-no")
        provider = CroPdfAttachmentProvider(settings, HttpClient(timeout=settings.app.request_timeout_seconds))
        logger.info("Testing CRO report fetch | cro_no=%s", args.cro_no)
        result = provider.fetch_attachment(args.cro_no)
        print(f"CRO report test: {args.cro_no}")
        print(f" - status: {result.status}")
        print(f" - hit: {result.hit}")
        print(f" - summary: {result.summary}")
        if result.errors:
            print(" - errors:")
            for error in result.errors:
                print(f"   - {error}")
        if result.data and result.data.bytes_content:
            output_path = args.output or (settings.app.output_dir / f"cro_{args.cro_no}.pdf")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(result.data.bytes_content)
            print(f" - saved: {output_path}")
        if result.raw is not None:
            print(" - raw response:")
            _safe_json_print(result.raw)

    if args.test_fir_report:
        missing_args = [name for name, value in {"--fir-no": args.fir_no, "--fir-year": args.fir_year, "--ps-id": args.ps_id}.items() if not value]
        if missing_args:
            raise SystemExit(f"--test-fir-report requires {' '.join(missing_args)}")
        provider = PsrmsFirAttachmentProvider(settings.providers.psrms, HttpClient(timeout=settings.app.request_timeout_seconds))
        logger.info("Testing FIR report fetch | fir_no=%s fir_year=%s ps_id=%s", args.fir_no, args.fir_year, args.ps_id)
        result = provider.fetch_attachment(args.fir_no, args.fir_year, args.ps_id)
        print(f"FIR report test: {args.fir_no}/{args.fir_year} PS#{args.ps_id}")
        print(f" - status: {result.status}")
        print(f" - hit: {result.hit}")
        print(f" - summary: {result.summary}")
        if result.errors:
            print(" - errors:")
            for error in result.errors:
                print(f"   - {error}")
        if result.data:
            if result.data.media_type == "pdf" and result.data.bytes_content:
                output_path = args.output or (settings.app.output_dir / f"fir_{args.fir_no}_{args.fir_year}_{args.ps_id}.pdf")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(result.data.bytes_content)
                print(f" - saved: {output_path}")
            elif result.data.media_type in {"html", "text"} and result.data.text_content:
                output_path = args.output or (settings.app.output_dir / f"fir_{args.fir_no}_{args.fir_year}_{args.ps_id}.html")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(result.data.text_content, encoding="utf-8")
                print(f" - saved: {output_path}")
                if result.data.metadata:
                    print(" - parsed metadata:")
                    _safe_json_print(result.data.metadata)
        if result.raw is not None:
            print(" - raw response:")
            _safe_json_print(result.raw)

    print("Core providers configured:")
    print(" - subscriber")
    print(" - prvs")
    print(" - cro")
    print(" - psrms")
    print(" - watchlist")
    print("Next step: run live provider tests, then keep polishing attachments and section rendering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
