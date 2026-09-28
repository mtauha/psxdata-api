import psxdata

# Historical OHLCV data
df = psxdata.stocks("ENGRO", start="2024-01-01", end="2024-12-31")

# All listed tickers
all_tickers = psxdata.tickers()

# KSE-100 index constituents
kse100 = psxdata.indices("KSE100")

# Live quote
q = psxdata.quote("LUCK")

# Sector summary
sectors = psxdata.sectors()

# Debt market instruments
debt = psxdata.debt_market()

# Margin-eligible stocks
scrips = psxdata.eligible_scrips()