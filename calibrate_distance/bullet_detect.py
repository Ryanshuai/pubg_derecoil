import cv2
import numpy as np


def im_to_deltaY(im):
    im[600:, :, :] = np.ones((len(im) - 600, len(im[0]), 3)) * 255
    # cv2.imshow("", im)
    # cv2.waitKey()

    im_gray = np.where(im > 0, 0, 255)
    im_gray = im_gray[:, :, 0] & im_gray[:, :, 1] & im_gray[:, :, 2]
    im_gray = im_gray.astype(np.uint8)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))  # 定义矩形结构元素
    b_im = cv2.morphologyEx(im_gray, cv2.MORPH_OPEN, kernel, iterations=1)  # 开运算1

    # cv2.imwrite("b_im.png", b_im)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(b_im, connectivity=8)
    areas = stats[:, -1]

    centroids = centroids[1:]
    centroids = centroids[centroids[:, 0].argsort()]
    Ys = centroids[:, 1]
    deltaY = np.diff(Ys)
    deltaY /= 3
    print(-deltaY.astype(np.int))

    # for x, y in centroids:
    #     cv2.circle(im, (int(x), int(y)), 2, (255, 0, 0), 6)
    # cv2.imshow("keypoints.png", im)
    # cv2.waitKey()

    return deltaY


if __name__ == '__main__':
    import os
    from calibrate_distance.write_dict import write_to_file_delta


    # im = cv2.imread("gun_dist_screen/qbz/1 (2).png")
    # im_to_deltaY(im)

    def write_delta(gun_name):
        gun_dir = os.path.join("gun_dist_screen", gun_name)
        deltaY_list = []
        min_len = 1000
        for im_name in os.listdir(gun_dir):
            im_path = os.path.join(gun_dir, im_name)
            im = cv2.imread(im_path)
            deltaY = im_to_deltaY(im)
            min_len = min(min_len, len(deltaY))
            deltaY_list.append(deltaY)

        m = len(deltaY_list)
        deltaY_mat = np.zeros((m, min_len))
        for i in range(m):
            deltaY_mat[i, :] = deltaY_list[i][:min_len]
        deltaY_avr = np.average(deltaY_mat, axis=0)
        deltaY_avr = np.round(deltaY_avr).astype(np.int)
        print(gun_dir, -deltaY_avr)
        write_to_file_delta(gun_name, -deltaY_avr)


    write_delta("m762")
