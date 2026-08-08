"""Real-time human/head detection + crosshair tracking.

CrosshairDetector: finds the scope crosshair via red-dot or projection.
TargetDetector: YOLO-based human/head detection in the scope crop.
AimBot: queries both, computes delta, moves mouse via Pico.
"""
import os
import time
import cv2
import numpy as np
from ultralytics import YOLO

from capture.cropper import win32_cap as _win32_cap_yxhw


def win32_cap(x, y, w, h):
    return _win32_cap_yxhw((y, x, h, w))


# ── Config ──────────────────────────────────────────────
import config
from config import SCREEN_W, SCREEN_H
CX, CY = SCREEN_W // 2, SCREEN_H // 2


class CrosshairDetector:
    """Finds crosshair position near screen center."""

    def __init__(self, left=40, right=40, up=100, down=40):
        self.left = left
        self.right = right
        self.up = up
        self.down = down
        self.x = CX
        self.y = CY
        self.method = ''

    def query(self, full_frame=None, frame_x0=0, frame_y0=0):
        """Detect crosshair. If full_frame given, extract region from it.
        frame_x0/y0: screen coords of full_frame's top-left corner.
        """
        x0 = CX - self.left
        y0 = CY - self.up
        w = self.left + self.right
        h = self.up + self.down

        if full_frame is not None:
            # Extract crosshair region from the larger frame
            rx = x0 - frame_x0
            ry = y0 - frame_y0
            frame = full_frame[ry:ry + h, rx:rx + w]
        else:
            frame = win32_cap(x0, y0, w, h)

        # Red dot detection
        r = frame[:, :, 2].astype(float)
        g = frame[:, :, 1].astype(float)
        b = frame[:, :, 0].astype(float)
        redness = r - np.maximum(g, b)
        if redness.max() > 30:
            idx = np.argmax(redness)
            cy, cx = np.unravel_index(idx, redness.shape)
            self.x = x0 + int(cx)
            self.y = y0 + int(cy)
            self.method = 'red'
            return

        # Fallback: projection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        inv = 255 - gray
        kernel = np.ones(3) / 3
        col_smooth = np.convolve(inv.sum(axis=0).astype(float), kernel, mode='same')
        row_smooth = np.convolve(inv.sum(axis=1).astype(float), kernel, mode='same')
        self.x = x0 + int(np.argmax(col_smooth))
        self.y = y0 + int(np.argmax(row_smooth))
        self.method = 'proj'


class TargetDetector:
    """YOLO-based human/head detection in the scope crop area."""

    CONF_BODY = config.CONF_BODY
    CONF_HEAD = config.CONF_HEAD
    PRONE_RATIO = 1.5

    def __init__(self, crop_w=800, crop_h=800,
                 model_path=None):
        self.crop_w = crop_w
        self.crop_h = crop_h
        if model_path is None:
            model_path = os.path.join(os.path.dirname(__file__), 'best.pt')
        self.model = YOLO(model_path)
        # Results (screen coords)
        self.x = None
        self.y = None
        # For visualization (crop coords)
        self.target_body = None
        self.best_head = None
        self.frame = None

    @property
    def crop_x0(self):
        return CX - self.crop_w // 2

    @property
    def crop_y0(self):
        return CY - self.crop_h // 2

    def query(self):
        """Capture and detect. Updates self.x, self.y (screen coords)."""
        x0 = self.crop_x0
        y0 = self.crop_y0
        self.frame = win32_cap(x0, y0, self.crop_w, self.crop_h)

        results = self.model.predict(self.frame, imgsz=640, conf=0.01,
                                     device=0, verbose=False)

        bodies = []
        heads = []
        for r in results:
            for box in r.boxes:
                xyxy = box.xyxy[0].cpu().numpy().astype(int)
                cls = int(box.cls[0])
                conf = float(box.conf[0])
                if cls == 0 and conf >= self.CONF_BODY:
                    bodies.append((*xyxy, conf))
                elif cls == 1 and conf >= self.CONF_HEAD:
                    heads.append((*xyxy, conf))

        if not bodies:
            self.x = self.y = None
            self.target_body = self.best_head = None
            return

        # Top-10 by conf, then closest to crop center
        img_cx, img_cy = self.crop_w // 2, self.crop_h // 2
        top10 = sorted(bodies, key=lambda b: b[4], reverse=True)[:10]
        self.target_body = min(top10, key=lambda b: (
            (b[0] + b[2]) / 2 - img_cx) ** 2 + ((b[1] + b[3]) / 2 - img_cy) ** 2)

        bx1, by1, bx2, by2, _ = self.target_body
        bw, bh = bx2 - bx1, by2 - by1

        # Best head inside this body
        self.best_head = None
        for hx1, hy1, hx2, hy2, hconf in heads:
            hcx, hcy = (hx1 + hx2) // 2, (hy1 + hy2) // 2
            if bx1 <= hcx <= bx2 and by1 <= hcy <= by2:
                if self.best_head is None or hconf > self.best_head[4]:
                    self.best_head = (hx1, hy1, hx2, hy2, hconf)

        if self.best_head:
            cx = (self.best_head[0] + self.best_head[2]) // 2
            cy = (self.best_head[1] + self.best_head[3]) // 2
        elif bw / max(bh, 1) > self.PRONE_RATIO:
            cx = (bx1 + bx2) // 2
            cy = (by1 + by2) // 2
        else:
            cx = (bx1 + bx2) // 2
            cy = by1 + int(bh * 0.15)

        # Convert to screen coords
        self.x = x0 + cx
        self.y = y0 + cy


# ── Visualization (standalone mode) ─────────────────────

COLORS = [(0, 255, 0), (0, 0, 255)]  # green=body, red=head


def main():
    xhair = CrosshairDetector()
    target = TargetDetector()
    print(f"Model loaded: {target.model.model_name}")

    cv2.namedWindow('detect', cv2.WINDOW_NORMAL)
    cv2.moveWindow('detect', SCREEN_W + 50, 50)
    cv2.resizeWindow('detect', target.crop_w, target.crop_h)

    while True:
        t0 = time.perf_counter()

        xhair.query()
        target.query()

        frame = target.frame
        x0 = target.crop_x0
        y0 = target.crop_y0

        # Draw crosshair search region and detected position
        rx0 = (CX - xhair.left) - x0
        ry0 = (CY - xhair.up) - y0
        rw = xhair.left + xhair.right
        rh = xhair.up + xhair.down
        cv2.rectangle(frame, (rx0, ry0), (rx0 + rw, ry0 + rh),
                      (0, 255, 255), 1)
        xh_crop_x = xhair.x - x0
        xh_crop_y = xhair.y - y0
        cv2.drawMarker(frame, (xh_crop_x, xh_crop_y),
                       (255, 255, 0), cv2.MARKER_CROSS, 30, 2)
        off_x = xhair.x - CX
        off_y = xhair.y - CY
        cv2.putText(frame, f'xhair/{xhair.method} ({off_x:+d},{off_y:+d})',
                    (xh_crop_x + 20, xh_crop_y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

        # Draw detections
        if target.target_body:
            bx1, by1, bx2, by2, bconf = target.target_body
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), COLORS[0], 2)
            cv2.putText(frame, f'body {bconf:.2f}', (bx1, by1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORS[0], 1)
        if target.best_head:
            hx1, hy1, hx2, hy2, hconf = target.best_head
            cv2.rectangle(frame, (hx1, hy1), (hx2, hy2), COLORS[1], 2)
            cv2.putText(frame, f'head {hconf:.2f}', (hx1, hy1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORS[1], 1)
        if target.x is not None:
            tx = target.x - x0
            ty = target.y - y0
            cv2.drawMarker(frame, (tx, ty), (0, 255, 255),
                           cv2.MARKER_CROSS, 20, 2)

            # Draw delta line from crosshair to target
            dx = target.x - xhair.x
            dy = target.y - xhair.y
            cv2.line(frame, (xh_crop_x, xh_crop_y), (tx, ty), (0, 165, 255), 2)
            cv2.putText(frame, f'delta ({dx:+d},{dy:+d})',
                        (tx + 20, ty + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)

        dt = time.perf_counter() - t0
        fps = 1.0 / dt if dt > 0 else 0
        cv2.putText(frame, f'{fps:.0f} FPS',
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

        cv2.imshow('detect', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
