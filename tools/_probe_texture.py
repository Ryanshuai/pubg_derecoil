"""Is there anything in the tracked patch worth correlating? -> float.

One author for a question three probes were asking with three copies of the
same sixteen lines (`probe_additivity`, `probe_correlator_bias`,
`probe_delivery_path`, merged 2026-08-12). Only the third copy carried the
paragraph below; the other two were the same arithmetic with the reason
stripped off, which is the shape this repo pays for -- a reader of either of
those two had nothing telling them a 0.0 is the dangerous answer.
"""
import numpy as np


def texture(rig, grabber):
    """Laplacian variance of the tracked patches. -> float.

    ⚠ THE CORRELATOR MEASURES HOW THE PICTURE MOVES, so a picture with nothing
    in it reads a confident zero rather than an error. A whole run went that
    way on 2026-08-08 -- the view was on open sky, within-arm CV came back at
    103%, and several trials read 0.0 px. Spotted from the chair ("你对的天空
    了，我不知道你能不能测出来啥"), not by anything in the program.

    This is the same shape as control/aim.py's clamp probe, which cannot use
    the tracker at either stop for exactly this reason: at the top there is
    sky, and BLIND READS AS "IT DID NOT MOVE".
    """
    import cv2
    for _ in range(3):
        grabber.grab_timed()
    _t, f = grabber.grab_timed()
    p = rig.tracker.slice_frame(f) if f is not None else None
    if p is None:
        return 0.0
    arrs = p if isinstance(p, (list, tuple)) else [p]
    vs = []
    for a in arrs:
        a = np.asarray(a)
        if a.ndim == 3:
            a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        vs.append(float(cv2.Laplacian(a.astype(np.uint8), cv2.CV_64F).var()))
    return float(np.median(vs))
