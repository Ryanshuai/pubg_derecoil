"""Closed-loop drivers: observe -> act -> verify.

The layer that knows what is happening in the game. Reads through `detector`,
acts through `press`, and re-reads to confirm — never assumes an action landed
because it was sent.

The dependency runs one way: control -> detector, control -> press. Nothing in
`detector` or `press` may import from here.
"""
