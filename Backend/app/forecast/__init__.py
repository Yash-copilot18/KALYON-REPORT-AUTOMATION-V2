"""Weather-aware day-ahead generation forecasting and deviation detection.

Self-contained: this package reads the existing SCADA tables through the
existing SQLAlchemy session and adds two tables of its own. It changes no
existing query, table or endpoint.
"""
