"""CLI entry point for the domestic travel planner."""

import argparse
from datetime import datetime


def validate_date(date_text):
	"""Return a valid date string in YYYY-MM-DD format."""
	try:
		parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
	except ValueError as error:
		raise argparse.ArgumentTypeError(
			"date must be a valid date in YYYY-MM-DD format"
		) from error

	return parsed_date.strftime("%Y-%m-%d")


def create_parser():
	parser = argparse.ArgumentParser(
		description="Get a domestic travel recommendation for a date."
	)
	parser.add_argument(
		"-date",
		"--date",
		dest="travel_date",
		required=True,
		type=validate_date,
		help="Travel date in YYYY-MM-DD format.",
	)
	return parser


def main():
	args = create_parser().parse_args()
	print(f"Travel date: {args.travel_date}")


if __name__ == "__main__":
	main()