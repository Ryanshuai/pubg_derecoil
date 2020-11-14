import cv2
import numpy as np
import os


def detect_y_move(img0, img, type='mode'):
    orb = cv2.ORB_create()
    kp0, des0 = orb.detectAndCompute(img0, None)
    kp, des = orb.detectAndCompute(img, None)

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.knnMatch(des0, des, k=1)

    while [] in matches:
        matches.remove([])
    matches = sorted(matches, key=lambda x: x[0].distance)

    dy_list = list()
    for match in matches[0:30]:
        x0, y0 = int(kp0[match[0].queryIdx].pt[0]), int(kp0[match[0].queryIdx].pt[1])
        x, y = int(kp[match[0].trainIdx].pt[0]), int(kp[match[0].trainIdx].pt[1])
        dy_list.append(y - y0)

        # img0 = cv2.circle(img0, (x0, y0), 5, (255, 255, 0))
        # img = cv2.circle(img, (x, y), 5, (255, 255, 0))
        # cv2.imshow('img0', img0)
        # cv2.imshow('img', img)
        # cv2.waitKey()
    dy_list = list(filter(lambda x: x > 0, dy_list))
    if dy_list:
        if type == 'mode':
            counts = np.bincount(dy_list)
            mode = np.argmax(counts)
            return mode
        else:
            return sum(dy_list[:10]) / 10
    return 0


def detect_dir(img_dir, type='mode'):
    y_move_array = np.zeros((50))
    for i in range(len(os.listdir(img_dir)) - 1):
        print('checking: ', img_dir, i, i + 1)
        im0 = cv2.imread(os.path.join(img_dir, str(i) + '.png'))
        im = cv2.imread(os.path.join(img_dir, str(i + 1) + '.png'))
        y_move = detect_y_move(im0, im, type)
        y_move_array[i] = y_move
    return y_move_array


if __name__ == '__main__':
    img1 = cv2.imread("image_match_dir/akm/1/31.png")
    img2 = cv2.imread("image_match_dir/akm/1/32.png")

    dy = detect_y_move(img1, img2)
    print(dy)

    # y_list = detect_dir('image_match_dir/m762/4/')
    # print(y_list)
