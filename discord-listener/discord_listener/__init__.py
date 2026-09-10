"""Kopyaa Discord listener — real-time message detection from Discord Web.

Runs as its own service, isolated from the rest of the platform: no database
connection, no broker credentials, no user session. Its only authority is the
shared listener token, which lets it fetch the channel assignments it should
watch and post the messages it observes back to the Kopyaa backend.
"""
