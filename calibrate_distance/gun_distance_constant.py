dist_lists = {'m762': [44, 24, 31, 31, 40, 40, 40, 40, 40, 53, 53, 53, 53, 53, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 55, 57, 57, 57, 57, 57, 57, 57, 57, 57, 57],
'akm': [42, 27, 27, 27, 33, 33, 33, 33, 33, 42, 42, 42, 42, 42, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47],
'aug': [6.2, 13.4, 18.0, 24.2, 26.2, 24.4, 24.4, 26.2, 25.2, 34.2, 25.8, 29.0, 30.6, 34.2, 30.4, 31.0, 31.6, 34.6, 26.6, 28.0, 34.4, 30.8, 28.4, 39.6, 24.0, 38.0, 28.2, 34.8, 23.4, 38.2, 29.8, 34.8, 29.8, 33.4, 32.6, 27.4, 34.4, 47.2, 32.4, 20.6, 46.0, 4.2, 0.2],
'dp28': [30, 20, 20, 20, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
'groza': [7.428571428571429, 20.0, 22.857142857142858, 26.857142857142858, 24.714285714285715, 25.428571428571427, 30.0, 21.857142857142858, 30.142857142857142, 34.0, 23.285714285714285, 29.142857142857142, 27.857142857142858, 21.285714285714285, 34.57142857142857, 36.0, 29.714285714285715, 49.57142857142857, 34.42857142857143, 37.0, 54.857142857142854, 38.57142857142857, 44.0, 34.0, 53.0, 28.714285714285715, 38.57142857142857, 51.714285714285715, 35.42857142857143, 34.57142857142857, 58.42857142857143, 4.0],
'm249': [19.714285714285715, 13.571428571428571, 16.428571428571427, 16.285714285714285, 22.714285714285715, 16.285714285714285, 30.285714285714285, 32.142857142857146, 43.42857142857143, 33.142857142857146, 32.857142857142854, 22.285714285714285, 18.571428571428573, 29.285714285714285, 24.857142857142858, 22.571428571428573, 26.857142857142858, 17.142857142857142, 15.142857142857142, 21.857142857142858, 14.571428571428571, 22.285714285714285, 15.285714285714286, 15.0, 17.285714285714285, 10.714285714285714, 22.571428571428573, 12.428571428571429, 10.285714285714286, 3.0, 3.142857142857143],
'm416': [35, 18, 18, 26, 26, 26, 26, 26, 26, 35, 35, 35, 35, 35, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 38, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40],
'qbz': [34, 18, 18, 18, 27, 27, 27, 27, 27, 35, 35, 35, 35, 35, 38, 38, 38, 38, 38, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43, 43],
'scar': [30, 20, 20, 20, 28, 28, 28, 28, 28, 33, 33, 33, 33, 33, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37],
'g36c': [40, 16, 16, 16, 26, 26, 26, 26, 26, 30, 30, 30, 30, 30, 34, 34, 34, 34, 34, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36, 36],
'pp19': [7.2, 14.8, 22.0, 20.4, 24.0, 24.2, 26.2, 24.2, 23.4, 26.6, 21.6, 24.6, 19.6, 23.2, 19.4, 23.8, 21.0, 21.0, 21.6, 22.0, 22.0, 20.6, 21.4, 18.2, 19.6, 21.2, 17.6, 23.4, 15.6, 24.4, 19.2, 16.6, 12.4, 11.4, 13.6, 12.0, 7.6, 4.4, 5.2, 3.8, 4.8],
'tommy': [20, 20, 21, 21, 21, 24, 24, 30, 30, 40, 40, 40, 40, 40, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45],
'uzi': [13, 12, 12, 12, 12, 12, 12, 12, 12, 20, 20, 20, 20, 20, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
'ump45': [18, 18, 18, 18, 27, 27, 27, 27, 27, 27, 27, 27, 27, 27, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
'vector': [16, 16, 16, 16, 16, 20, 20, 20, 20, 24, 24, 24, 28, 28, 32, 32, 32, 32, 32, 34, 34, 34, 34, 34, 34, 34, 34, 34, 34, 34, 34, 34],
'mp5k': [15.0, 31.8, 21.4, 27.0, 36.4, 23.6, 25.2, 31.6, 35.2, 30.0, 28.2, 32.2, 29.4, 27.8, 24.0, 32.4, 26.4, 28.6, 25.2, 25.8, 30.0, 28.4, 25.4, 34.0, 31.2, 28.2, 24.0, 30.6, 26.2, 31.0, 28.2, 23.4, 27.0, 29.4, 58.0, 16.8, 16.4, 48.0, 6.2, 5.2, 0.6, 1.0, 0.6],
'mini14': [9],
'qbu': [10],
'sks': [12],
'slr': [8.5],
'mk14': [16],
's686': [100],
's12k': [80],
}