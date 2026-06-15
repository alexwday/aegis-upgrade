"""
Fiscal period generator for dynamic date context.
"""

from datetime import datetime, timedelta


def _get_quarter_dates(fiscal_year: int, quarter: int) -> tuple:
    """
    Calculate start and end dates for a fiscal quarter.

    Args:
        fiscal_year: The fiscal year
        quarter: Quarter number (1-4)

    Returns:
        Tuple of (start_date, end_date) as datetime objects
    """
    fiscal_start_month = 11  # November
    quarter_start_month = fiscal_start_month + (quarter - 1) * 3

    if quarter_start_month > 12:
        quarter_start_month -= 12
        quarter_year = fiscal_year
    else:
        quarter_year = fiscal_year - 1

    quarter_start = datetime(quarter_year, quarter_start_month, 1)

    # Calculate quarter end (last day of third month in quarter)
    quarter_end_month = quarter_start_month + 2
    if quarter_end_month > 12:
        quarter_end_month -= 12
        quarter_end_year = quarter_year + 1
    else:
        quarter_end_year = quarter_year

    # Get last day of quarter end month
    if quarter_end_month == 12:
        quarter_end = datetime(quarter_end_year, 12, 31)
    else:
        next_month = datetime(quarter_end_year, quarter_end_month + 1, 1)
        quarter_end = next_month - timedelta(days=1)

    return quarter_start, quarter_end


def _get_fiscal_year_and_quarter(current_date: datetime) -> tuple:
    """
    Calculate fiscal year and quarter from a date.

    Args:
        current_date: The date to calculate from

    Returns:
        Tuple of (fiscal_year, quarter)
    """
    fiscal_start_month = 11  # November

    # Calculate fiscal year
    if current_date.month >= fiscal_start_month:
        fiscal_year = current_date.year + 1
        months_elapsed = current_date.month - fiscal_start_month
    else:
        fiscal_year = current_date.year
        months_elapsed = (12 - fiscal_start_month) + current_date.month

    quarter = (months_elapsed // 3) + 1
    return fiscal_year, quarter


def _build_quarters_info(fiscal_year: int) -> list:
    """
    Build quarter information strings for all quarters.

    Args:
        fiscal_year: The fiscal year

    Returns:
        List of formatted quarter strings
    """
    quarters_info = []
    quarter_names = ["Q1 (Nov-Jan)", "Q2 (Feb-Apr)", "Q3 (May-Jul)", "Q4 (Aug-Oct)"]

    for q_num in range(1, 5):
        q_start, q_end = _get_quarter_dates(fiscal_year, q_num)
        quarters_info.append(
            f"  - {quarter_names[q_num-1]}: "
            f"{q_start.strftime('%b %d, %Y')} to {q_end.strftime('%b %d, %Y')}"
        )

    return quarters_info


def get_fiscal_statement(current_date: datetime = None) -> str:
    """
    Generate fiscal period statement based on current date.

    Fiscal year: November 1 - October 31

    Args:
        current_date: Optional datetime for testing (defaults to now)

    Returns:
        Formatted fiscal period context string
    """
    # Use provided date or current date
    current_date = current_date or datetime.now()

    # Get fiscal year and quarter
    fiscal_year, quarter = _get_fiscal_year_and_quarter(current_date)

    # Fiscal year start date
    fy_start = datetime(fiscal_year - 1, 11, 1)  # November 1st

    # Current quarter dates
    quarter_start, quarter_end = _get_quarter_dates(fiscal_year, quarter)

    # Days calculations
    days_remaining = (quarter_end - current_date).days + 1
    days_elapsed = (current_date - quarter_start).days + 1

    # Build all quarters info
    quarters_info = _build_quarters_info(fiscal_year)

    # Generate the fiscal statement
    statement = f"""Fiscal Period Context:

Today's Date: {current_date.strftime('%B %d, %Y')}
Current Fiscal Year: FY{fiscal_year} (Nov 1, {fiscal_year-1} - Oct 31, {fiscal_year})
Current Fiscal Quarter: FY{fiscal_year} Q{quarter}

Current Quarter:
  - Period: {quarter_start.strftime('%B %d, %Y')} to {quarter_end.strftime('%B %d, %Y')}
  - Days Remaining: {days_remaining}
  - Days Elapsed: {days_elapsed}

Fiscal Year Quarters:
{chr(10).join(quarters_info)}

Date Reference Guidelines:
  - Year-to-date (YTD): From {fy_start.strftime('%B %d, %Y')} to today
  - Quarter-to-date (QTD): From {quarter_start.strftime('%B %d, %Y')} to today
  - Prior year comparison: FY{fiscal_year-1} (Nov 1, {fiscal_year-2} - Oct 31, {fiscal_year-1})
  - Use current fiscal period unless specifically requested otherwise"""

    return statement


if __name__ == "__main__":
    # Test the function
    print(get_fiscal_statement())
