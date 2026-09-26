import csv


def get_conference_city_in_order(env, config):
    csv_path = config['csv_path']
    print(f"Reading csv file from {csv_path}")
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
    next(reader)
    conference_city_list = [row[2] for row in reader]
    return conference_city_list
