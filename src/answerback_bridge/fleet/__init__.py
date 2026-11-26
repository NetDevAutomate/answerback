"""The fleet document writer: measures the fleet and publishes one ``fleet.json``.

Pure parts (bounds, scale, cadence, advice, receipts, document) read no clock, file or
process. Effect parts (probes, service, publish, loop) get their clock and runner passed in.
"""
