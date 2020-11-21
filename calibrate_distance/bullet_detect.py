import cv2
import numpy as np
from calibrate_distance.write_dict import write_to_file_delta


def im_to_deltaY(im):
    im_gray = np.where(im > 0, 0, 255)
    im_gray = im_gray[:, :, 0] & im_gray[:, :, 1] & im_gray[:, :, 2]
    im_gray = im_gray.astype(np.uint8)

    im_gray[620:, 1570:1881] = np.zeros((len(im_gray) - 620, 1881 - 1570))

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))  # 定义矩形结构元素
    b_im = cv2.morphologyEx(im_gray, cv2.MORPH_OPEN, kernel, iterations=1)  # 开运算1

    # cv2.imwrite("b_im.png", b_im)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(b_im, connectivity=8)
    areas = stats[:, -1]

    centroids = centroids[1:]
    # for x, y in centroids:
    #     cv2.circle(im, (int(x), int(y)), 2, (255, 0, 0), 6)
    # cv2.imwrite("keypoints.png", im)

    centroids = centroids[centroids[:, 0].argsort()]
    Ys = centroids[:, 1]
    deltaY = np.diff(Ys)
    deltaY = np.round(deltaY)

    return deltaY.astype(np.int)


if __name__ == '__main__':
    import os


    def write_delta(gun_name):
        deltaY_list = []
        for im_name in os.listdir(gun_name):
            im_path = os.path.join(gun_name, im_name)
            im = cv2.imread(im_path)
            deltaY = im_to_deltaY(im)
            deltaY_list.append(deltaY)
        print()


    im = cv2.imread("6.png")
    deltaY = im_to_deltaY(im)
    print(deltaY)
