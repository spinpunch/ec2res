#!/usr/bin/python

# Report coverage of reserved instances in Amazon EC2 and RDS.

# This script checks for running instances that are not covered by reservations,
# as well as any reservations that aren't being used by a running instance.
# It also reports any upcoming service events that will affect EC2 instances.

# This is a stand-alone script with no dependencies other than Amazon's "boto" library.

# Copyright (c) 2015 Battlehouse Inc. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# Updated for public release by Dan Maas.

import sys, time, datetime, calendar, getopt
import boto.ec2, boto.rds2
import boto3

time_now = int(time.time())

class ANSIColor:
    """ For colorizing the terminal output """
    BOLD = '\033[1m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    @classmethod
    def bold(self, x): return self.BOLD+x+self.ENDC
    @classmethod
    def red(self, x): return self.RED+x+self.ENDC
    @classmethod
    def green(self, x): return self.GREEN+x+self.ENDC
    @classmethod
    def yellow(self, x): return self.YELLOW+x+self.ENDC

def decode_time_string(amztime):
    """ Translate Amazon API time string to a UNIX timestamp """
    return calendar.timegm(time.strptime(amztime.split('.')[0], '%Y-%m-%dT%H:%M:%S'))

# Utility to convert a Python datetime to UNIX timestamp

ZERO = datetime.timedelta(0)
class DummyPythonUTC(datetime.tzinfo):
    def utcoffset(self, dt): return ZERO
    def tzname(self, dt): return "UTC"
    def dst(self, dt): return ZERO
dummy_python_utc = DummyPythonUTC()

def decode_time_datetime(dt):
    """ Translate Python datetime to UNIX timestamp """
    ret = dt - datetime.datetime(1970, 1, 1, tzinfo=dummy_python_utc)
    if hasattr(ret, 'total_seconds'):
        return ret.total_seconds()
    # Python 2.6 compatibility
    return (ret.microseconds + (ret.seconds + ret.days * 24 * 3600) * 10**6) / 10**6

# Utilities that operate on objects returned from the AWS API queries

def ec2_inst_is_vpc(inst):
    """ Is this EC2 instance a VPC instance? """
    return (inst.vpc_id is not None)

def ec2_res_is_vpc(res):
    """ Is this EC2 reservation for a VPC instance? """
    return ('VPC' in res['ProductDescription'])

def ec2_res_match(res, inst):
    """ Return true if this EC2 reservation can cover this EC2 instance. """
    if res['Scope'] == 'Availability Zone':
        if res['AvailabilityZone'] != inst.placement:
            return False
    elif res['Scope'] == 'Region':
        pass
    else:
        raise Exception('unknown scope for %r' % res)

    return res['InstanceType'] == inst.instance_type and \
           ec2_res_is_vpc(res) == ec2_inst_is_vpc(inst) and \
           res['State'] == 'active'

def rds_product_engine_match(product, engine):
    """ Check whether an RDS reservation 'product' matches a running instance 'engine' """
    return (product, engine) in (('postgresql','postgres'),
                                 ('mysql','mysql'), # note: not sure if this is correct
                                 )

def rds_res_match(res, inst, rds_offerings):
    """ Return true if this RDS reservation can cover this RDS instance. """
    # note: RDS uses slightly different terminology for the reservation "product" vs. the instance "engine"
    if 'ProductDescription' in res:
        product = res['ProductDescription']
    elif res['ReservedDBInstancesOfferingId'] in rds_offerings:
        product = rds_offerings[res['ReservedDBInstancesOfferingId']]['ProductDescription']
    else:
        # no way to find the "product" type
        return False

    engine = inst['Engine']
    return res['DBInstanceClass'] == inst['DBInstanceClass'] and \
           rds_product_engine_match(product, engine) and \
           res['MultiAZ'] == inst['MultiAZ']

# Pretty-printing utilities for objects returned from the AWS API

def pretty_print_ec2_res_price(res):
    """ Pretty-print price of an EC2 reservation """
    yearly = float(res['FixedPrice']) * (365*86400)/float(res['Duration'])
    for charge in res['RecurringCharges']:
        assert charge['Frequency'] == 'Hourly'
        yearly += float(charge['Amount']) * (365*24)
    yearly += float(res['UsagePrice']) * (365*24) # ???
    return '$%.0f/yr' % yearly

def pretty_print_ec2_res_where(res):
    """ Pretty-print zonal placement of an EC2 reservation """
    if res['Scope'] == 'Region':
        return '(region)'
    elif res['Scope'] == 'Availability Zone':
        return res['AvailabilityZone']
    else:
        raise Exception('unknown where %r' % res)

def pretty_print_ec2_res(res, override_count = None, my_index = None):
    """ Pretty-print an entire EC2 reservation """
    assert res['State'] == 'active'
    lifetime = decode_time_datetime(res['Start']) + res['Duration'] - time_now
    days = lifetime//86400
    is_vpc = '--VPC--' if ec2_res_is_vpc(res) else 'Classic'
    if my_index is not None and res['InstanceCount'] > 1:
        count = ' (%d of %d)' % (my_index+1, res['InstanceCount'])
    else:
        instance_count = override_count if override_count is not None else res['InstanceCount']
        count = ' (x%d)' % instance_count if (instance_count!=1 or override_count is not None) else ''
    return '%-10s %-7s %-22s %10s  %3d days left' % (pretty_print_ec2_res_where(res), is_vpc, res['InstanceType']+count, pretty_print_ec2_res_price(res), days)

def pretty_print_ec2_res_id(res):
    """ Pretty-print the EC2 reservation ID """
    return res['ReservedInstancesId'].split('-')[0]+'...'

def pretty_print_ec2_instance(inst):
    """ Pretty-print a running EC2 instance """
    is_vpc = '--VPC--' if ec2_inst_is_vpc(inst) else 'Classic'
    return '%-24s %-10s %-7s %-11s' % (inst.tags['Name'], inst.placement, is_vpc, inst.instance_type)

def pretty_print_rds_offering_price(offer):
    """ Pretty-print the price of an RDS reserved offering """
    yearly = float(offer['FixedPrice']) * (365*86400)/float(offer['Duration'])
    for charge in offer['RecurringCharges']:
        assert charge['RecurringChargeFrequency'] == 'Hourly'
        yearly += float(charge['RecurringChargeAmount']) * (365*24)
    yearly += float(offer['UsagePrice']) * (365*24) # ???
    return '$%.0f/yr' % yearly

def pretty_print_multiaz(flag):
    return 'MultiAZ' if flag else 'NoMulti'

def pretty_print_rds_res(res, rds_offerings, override_count = None, my_index = None):
    """ Pretty-print an RDS reservation """
    lifetime = res['StartTime'] + res['Duration'] - time_now
    days = lifetime//86400
    if my_index is not None and res['DBInstanceCount'] > 1:
        count = ' (%d of %d)' % (my_index+1, res['DBInstanceCount'])
    else:
        instance_count = override_count if override_count is not None else res['DBInstanceCount']
        count = ' (x%d)' % instance_count if (instance_count!=1 or override_count is not None) else ''
    #offer = rds_offerings.get(res['ReservedDBInstancesOfferingId'])
    return '%s %-22s %-12s %10s  %3d days left' % (pretty_print_multiaz(res['MultiAZ']), res['DBInstanceClass']+count,
                                                   res['ProductDescription'], # offer['ProductDescription'] if offer else 'UNKNOWN'),
                                                   pretty_print_rds_offering_price(res), # pretty_print_rds_offering_price(offer) if offer else '?',
                                                   days)

def pretty_print_rds_instance(inst):
    """ Pretty-print a running RDS instance """
    return '%-16s %-10s %s %-13s %-8s' % (inst['DBInstanceIdentifier'], inst['AvailabilityZone'], pretty_print_multiaz(inst['MultiAZ']), inst['DBInstanceClass'], inst['Engine'])

def get_rds_res_offerings(rds):
    """ Query RDS API for the reserved offerings """
    ret = {}
    marker = None
    while True:
        r = rds.describe_reserved_db_instances_offerings(marker=marker)['DescribeReservedDBInstancesOfferingsResponse']['DescribeReservedDBInstancesOfferingsResult']
        rlist = r['ReservedDBInstancesOfferings']
        marker = r['Marker']
        for x in rlist: ret[x['ReservedDBInstancesOfferingId']] = x
        if not rlist or not marker:
            break
    return ret

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], 'v', ['region='])
    verbose = False
    region = 'us-east-1'
    for key, val in opts:
        if key == '-v': verbose = True
        elif key == '--region': region = val

    conn = boto.ec2.connect_to_region(region)
    conn3 = boto3.client('ec2', region_name = region)
    rds = boto.rds2.connect_to_region(region)

    # query EC2 instances and reservations
    ec2_instance_list = conn.get_only_instances()
    ec2_res_list = conn3.describe_reserved_instances(Filters = [{'Name':'state','Values':['active']}])['ReservedInstances']
    ec2_status_list = conn.get_all_instance_status()

    # query RDS instances and reservations
    rds_instance_list = rds.describe_db_instances()['DescribeDBInstancesResponse']['DescribeDBInstancesResult']['DBInstances']
    rds_res_list = rds.describe_reserved_db_instances()['DescribeReservedDBInstancesResponse']['DescribeReservedDBInstancesResult']['ReservedDBInstances']

    rds_res_offerings = get_rds_res_offerings(rds)

    # only show running instances, and sort by name
    ec2_instance_list = sorted(filter(lambda x: x.state=='running' and not x.spot_instance_request_id,
                                      ec2_instance_list), key = lambda x: x.tags['Name'])
    rds_instance_list.sort(key = lambda x: x['DBInstanceIdentifier'])

    # disregard expired reservations
    ec2_res_list = filter(lambda x: x['State']=='active', ec2_res_list)
    rds_res_list = filter(lambda x: x['State']=='active', rds_res_list)

    # maps instance ID -> reservation that covers it
    ec2_res_coverage = dict((inst.id, None) for inst in ec2_instance_list)
    rds_res_coverage = dict((inst['DBInstanceIdentifier'], None) for inst in rds_instance_list)

    # maps reservation ID -> list of instances that it covers
    ec2_res_usage = dict((res['ReservedInstancesId'], []) for res in ec2_res_list)
    rds_res_usage = dict((res['ReservedDBInstanceId'], []) for res in rds_res_list)

    # figure out which instances are currently covered by reservations
    for res in ec2_res_list:
        for i in xrange(res['InstanceCount']):
            for inst in ec2_instance_list:
                if ec2_res_coverage[inst.id]: continue # instance already covered
                if ec2_res_match(res, inst):
                    ec2_res_coverage[inst.id] = res
                    ec2_res_usage[res['ReservedInstancesId']].append(inst)
                    break

    for res in rds_res_list:
        for i in xrange(res['DBInstanceCount']):
            for inst in rds_instance_list:
                if rds_res_coverage[inst['DBInstanceIdentifier']]: continue # instance already covered
                if rds_res_match(res, inst, rds_res_offerings):
                    rds_res_coverage[inst['DBInstanceIdentifier']] = res
                    rds_res_usage[res['ReservedDBInstanceId']].append(inst)
                    break

    # map instance ID -> upcoming service events
    ec2_instance_status = {}
    for stat in ec2_status_list:
        if stat.events:
            for event in stat.events:
                if '[Canceled]' in event.description or '[Completed]' in event.description: continue
                if stat.id not in ec2_instance_status: ec2_instance_status[stat.id] = []
                msg = event.description
                for timestring in (event.not_before,): # event.not_after):
                    ts = decode_time_string(timestring)
                    days_until = (ts - time_now)//86400
                    st = time.gmtime(ts)
                    msg += ' in %d days (%s/%d)' % (days_until, st.tm_mon, st.tm_mday)
                ec2_instance_status[stat.id].append(msg)

    # print console output

    print 'EC2 INSTANCES:'
    for inst in ec2_instance_list:
        res = ec2_res_coverage[inst.id]
        if res:
            my_index = ec2_res_usage[res['ReservedInstancesId']].index(inst)
            print ANSIColor.green(pretty_print_ec2_instance(inst)+' '+pretty_print_ec2_res(res, my_index = my_index)), pretty_print_ec2_res_id(res),
        else:
            print ANSIColor.red(pretty_print_ec2_instance(inst)+' NOT COVERED'),
        if inst.id in ec2_instance_status:
            print ANSIColor.yellow('EVENTS! '+','.join(ec2_instance_status[inst.id])),
        print

    ec2_any_unused = False
    print 'EC2 UNUSED RESERVATIONS:',
    for res in ec2_res_list:
        use_count = len(ec2_res_usage[res['ReservedInstancesId']])
        if use_count >= res['InstanceCount']: continue
        if not ec2_any_unused:
            print
            ec2_any_unused = True
        print ANSIColor.red(pretty_print_ec2_res(res, override_count = res['InstanceCount'] - use_count)), pretty_print_ec2_res_id(res)
    if not ec2_any_unused:
        print '(none)'

    print 'RDS INSTANCES:'
    for inst in rds_instance_list:
        res = rds_res_coverage[inst['DBInstanceIdentifier']]
        if res:
            my_index = rds_res_usage[res['ReservedDBInstanceId']].index(inst)
            print ANSIColor.green(pretty_print_rds_instance(inst)+' '+pretty_print_rds_res(res, rds_res_offerings, my_index = my_index)),
        else:
            print ANSIColor.red(pretty_print_rds_instance(inst)+' NOT COVERED'),
        print

    rds_any_unused = False
    print 'RDS UNUSED RESERVATIONS:',
    for res in rds_res_list:
        use_count = len(rds_res_usage[res['ReservedDBInstanceId']])
        if use_count >= res['DBInstanceCount']: continue
        if not rds_any_unused:
            print
            rds_any_unused = True
        print ANSIColor.red(pretty_print_rds_res(res, rds_res_offerings, override_count = res['DBInstanceCount'] - use_count)), res['ReservedDBInstanceId']
    if not rds_any_unused:
        print '(none)'
