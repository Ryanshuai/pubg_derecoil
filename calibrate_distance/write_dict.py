from calibrate_distance.gun_distance_constant import dist_lists


def write_to_file(write_dict, name):
    fileObject = open(name, 'w')
    fileObject.write('dist_lists = {')
    for k, v in write_dict.items():
        fileObject.write("'{}': {},".format(k, v))
        fileObject.write('\n')
    fileObject.write('}')
    fileObject.close()


def write_to_file_abs(gun_name, new_dist):
    dist_lists[gun_name] = list(new_dist)
    write_to_file(dist_lists, 'gun_distance_constant.py')


def write_to_file_average(gun_name, new_dist):
    num, dist = dist_lists.get(gun_name, [0])
    if len(new_dist) > len(dist):
        dist += new_dist[len(dist):]
    else:
        new_dist += dist[len(new_dist):]

    avr_dist = [(a * num + b) / (num + 1) for a, b in zip(dist, new_dist)]
    dist_lists[gun_name] = [num + 1, avr_dist]
    write_to_file(dist_lists, 'gun_distance_constant.py')


def delta_write_to_file_average(gun_name, delta_dist):
    num, dist = dist_lists[gun_name]
    if len(delta_dist) > len(dist):
        dist += delta_dist[len(dist):]
    else:
        delta_dist += dist[len(delta_dist):]

    avr_dist = [(a * num + a + delta) / (num + 1) for a, delta in zip(dist, delta_dist)]
    dist_lists[gun_name] = [num + 1, avr_dist]
    write_to_file(dist_lists, 'gun_distance_constant.py')


if __name__ == '__main__':
    write_to_file_average('akm', [47])
