#!/usr/bin/env python

# Erwan Quelin - erwan.quelin@gmail.com

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import VmomiSupport, SoapStubAdapter, vim, vmodl

from multiprocessing import Process

from pprint import pprint
import argparse
import atexit
import getpass
from datetime import datetime, timedelta
import time
import ssl

import vsanapiutils
import vsanmgmtObjects
import json
import pdb

import constants

def get_args():
    parser = argparse.ArgumentParser(
        description='Export vSAN cluster performance and storage usage statistics to InfluxDB line protocol')

    parser.add_argument('-s', '--vcenter',
                        required=True,
                        action='store',
                        help='Remote vcenter to connect to')

    parser.add_argument('-o', '--port',
                        type=int,
                        default=443,
                        action='store',
                        help='Port to connect on')

    parser.add_argument('-u', '--user',
                        required=True,
                        action='store',
                        help='User name to use when connecting to vcenter')

    parser.add_argument('-p', '--password',
                        required=False,
                        action='store',
                        help='Password to use when connecting to vcenter')

    parser.add_argument('-c', '--cluster_name',
                        dest='clusterName',
                        required=True,
                        help='Cluster Name')

    parser.add_argument('-esx_user', '--esx_username',
                        dest='esxUsername',
                        required=False,
                        help='ESX username')
    parser.add_argument('-esx_pswd', '--esx_password',
                        dest='esxPassword',
                        required=False,
                        help='ESX password')
    parser.add_argument("--performance",
                        help="Output performance metrics",
                        action="store_true")

    parser.add_argument("--capacity",
                        help="Output storage usage metrics",
                        action="store_true")

    parser.add_argument("--health",
                        help="Output cluster health status",
                        action="store_true")

    parser.add_argument("--disks",
                        help="Output vms disks status",
                        action="store_true")

    parser.add_argument('--skipentitytypes',
                        required=False,
                        action='store',
                        help='List of entity types to skip. Separated by a comma')

    args = parser.parse_args()

    if not args.password:
        args.password = getpass.getpass(
            prompt='Enter password for host %s and user %s: ' %
                   (args.vcenter, args.user))

    if not args.performance and args.skipentitytypes:
        print("You can't skip a performance entity type if you don't provide the --performance tag")
        exit()

    if not args.performance and not args.capacity and not args.health and not args.disks:
        print('Please provide tag(s) --performance and/or --capacity and/or --health to specify what type of data you want to collect')
        exit()

    return args


# Get cluster informations
def getClusterInstance(clusterName, content):
    searchIndex = content.searchIndex
    datacenters = content.rootFolder.childEntity
    for datacenter in datacenters:
        cluster = searchIndex.FindChild(datacenter.hostFolder, clusterName)
        if cluster is not None:
            return cluster
    return None

def get_obj(content, vim_type, name=None):
    obj = None
    container = content.viewManager.CreateContainerView(
        content.RootFolder, vim_type, True)
    if name:
        for c in container.view:
            if c.name == name:
                obj = c
                return obj
    else:
        return container.view

def getInformations(witnessHosts, cluster):

    uuid = {}
    hostnames = {}
    disks = {}

    # Get Host and disks informations
    for host in cluster.host:

        # Get relationship between host id and hostname
        hostnames[host.summary.host] = host.summary.config.name

        # Get all disk (cache and capcity) attached to hosts in the cluster
        diskAll = host.configManager.vsanSystem.QueryDisksForVsan()

        for disk in diskAll:
            if disk.state == 'inUse':
                uuid[disk.vsanUuid] = disk.disk.canonicalName
                disks[disk.vsanUuid] = host.summary.config.name

    for vsanHostConfig in cluster.configurationEx.vsanHostConfig:
        uuid[vsanHostConfig.clusterInfo.nodeUuid] = hostnames[vsanHostConfig.hostSystem]

    # Get witness disks informations

    return uuid, disks


# Get hosts informations (hostname and disks)
def getHostsInfos(cluster):
    disksinfos = {}
    hostnames = {}
    hostinfos = {}

    for host in cluster.host:
        hostnames[host.summary.host] = host.summary.config.name

        diskAll = host.configManager.vsanSystem.QueryDisksForVsan()

        for disk in diskAll:
            if disk.state == 'inUse':
                disksinfos[disk.vsanUuid] = disk.disk.canonicalName

    for vsanHostConfig in cluster.configurationEx.vsanHostConfig:
        hostinfos[vsanHostConfig.clusterInfo.nodeUuid] = hostnames[vsanHostConfig.hostSystem]

    return disksinfos, hostinfos


# Get all VM managed by the hosts in the cluster, return array with name and uuid of the VMs
# Source: https://github.com/vmware/pyvmomi-community-samples/blob/master/samples/getvmsbycluster.py
def getVMs(cluster):

    vms = {}

    for host in cluster.host:  # Iterate through Hosts in the Cluster
        for vm in host.vm:  # Iterate through each VM on the host
            vms[vm.summary.config.instanceUuid] = vm.summary.config.name

    return vms


# Output data in the Influx Line protocol format
def printInfluxLineProtocol(measurement, tags, fields, timestamp):
    result = "%s,%s %s %i" % (measurement, arrayToString(tags), arrayToString(fields), timestamp)
    print(result)


# Output data in the Influx Line protocol format
def formatInfluxLineProtocol(measurement, tags, fields, timestamp):
    result = "%s,%s %s %i \n" % (measurement, arrayToString(tags), arrayToString(fields), timestamp)
    return result


# Convert time in string format to epoch timestamp (nanosecond)
def convertStrToTimestamp(str):
    sec = time.mktime(datetime.strptime(str, "%Y-%m-%d %H:%M:%S").timetuple())

    ns = int(sec * 1000000000)

    return ns


# parse EntytyRefID, convert to tags
def parseEntityRefId(measurement, entityRefId, uuid, vms, disks):
    tags = {}

    if measurement == 'vscsi':
        entityRefId = entityRefId.split("|")
        split = entityRefId[0].split(":")

        tags['uuid'] = split[1]
        tags['vscsi'] = entityRefId[1]
        tags['vmname'] = vms[split[1]]
    else:
        entityRefId = entityRefId.split(":")

        if measurement == 'cluster-domclient':
            tags['uuid'] = entityRefId[1]

        if measurement == 'cluster-domcompmgr':
            tags['uuid'] = entityRefId[1]

        if measurement == 'host-domclient':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid[entityRefId[1]]

        if measurement == 'host-domcompmgr':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid[entityRefId[1]]

        if measurement == 'cache-disk':
            tags['uuid'] = entityRefId[1]
            tags['naa'] = uuid[entityRefId[1]]
            tags['hostname'] = disks[entityRefId[1]]

        if measurement == 'capacity-disk':
            tags['uuid'] = entityRefId[1]
            tags['naa'] = uuid[entityRefId[1]]
            tags['hostname'] = disks[entityRefId[1]]

        if measurement == 'disk-group':
            tags['uuid'] = entityRefId[1]

        if measurement == 'virtual-machine':
            tags['uuid'] = entityRefId[1]
            tags['vmname'] = vms[entityRefId[1]]

        if measurement == 'virtual-disk':
            split = entityRefId[1].split("/")

            tags['uuid'] = split[0]
            tags['disk'] = split[1]

        if measurement == 'vsan-vnic-net':
            split = entityRefId[1].split("|")

            tags['uuid'] = split[0]
            tags['hostname'] = uuid[split[0]]
            tags['stack'] = split[1]
            tags['vmk'] = split[2]

        if measurement == 'vsan-host-net':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid[entityRefId[1]]

        if measurement == 'vsan-pnic-net':

            split = entityRefId[1].split("|")

            tags['uuid'] = split[0]
            tags['hostname'] = uuid[split[0]]
            tags['vmnic'] = split[1]

        if measurement == 'vsan-iscsi-host':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid.get(entityRefId[1])

        if measurement == 'vsan-iscsi-target':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid.get(entityRefId[1])

        if measurement == 'vsan-iscsi-lun':
            tags['uuid'] = entityRefId[1]
            tags['hostname'] = uuid.get(entityRefId[1])

    return tags


# Convert array to a string compatible with influxdb line protocol tags or fields
def arrayToString(data):
    i = 0
    result = ""

    for key, val in data.items():
        # v = val.replace(' ', '\ ')
        v = val
        if isinstance(v, basestring):
            v = val.replace(' ', '\ ')
        t = 'i' if isinstance(val, (int, long)) else ''
        if i == 0:
            result = "%s=%s%s" % (key, v, t)
        else:
            result = result + ",%s=%s%s" % (key, v, t)
        i = i + 1
    return result

# Generate measurement with brand prefix
def genMeasurementName(mtype, scope):
    return "%s_%s_%s" % ('prevensys_vsan', mtype, scope)

def parseVsanObjectSpaceSummary(data):
    fields = {}

    fields['overheadB'] = data.overheadB
    fields['overReservedB'] = data.overReservedB
    fields['physicalUsedB'] = data.physicalUsedB
    fields['primaryCapacityB'] = data.primaryCapacityB
    fields['reservedCapacityB'] = data.reservedCapacityB
    fields['temporaryOverheadB'] = data.temporaryOverheadB
    fields['usedB'] = data.usedB

    if data.provisionCapacityB:
        fields['provisionCapacityB'] = data.provisionCapacityB

    return fields


def parseVimVsanDataEfficiencyCapacityState(data):
    fields = {}

    fields['dedupMetadataSize'] = data.dedupMetadataSize
    fields['logicalCapacity'] = data.logicalCapacity
    fields['logicalCapacityUsed'] = data.logicalCapacityUsed
    fields['physicalCapacity'] = data.physicalCapacity
    fields['physicalCapacityUsed'] = data.physicalCapacityUsed
    fields['ratio'] = float(data.logicalCapacityUsed) / float(data.physicalCapacityUsed)

    return fields


def parseCapacity(scope, data, tagsbase, timestamp):

    tags = {}
    fields = {}

    tags['scope'] = scope
    tags.update(tagsbase)
    # measurement = 'capacity_' + scope
    measurement = genMeasurementName('capacity', scope)

    if scope == 'global':
        fields['freeCapacityB'] = data.freeCapacityB
        fields['totalCapacityB'] = data.totalCapacityB

    elif scope == 'summary':
        fields = parseVsanObjectSpaceSummary(data.spaceOverview)

    elif scope == 'efficientcapacity':
        fields = parseVimVsanDataEfficiencyCapacityState(data.efficientCapacity)
    else:
        fields = parseVsanObjectSpaceSummary(data)

    printInfluxLineProtocol(measurement, tags, fields, timestamp)


def parseHealth(test, value, tagsbase, timestamp):

    # measurement = 'health_' + test
    measurement = genMeasurementName('health', test)
    values_dict = {
        'skipped': -1, 'green': 0, 'yellow': 1, 'red': 2, 'info': 3
    }

    tags = tagsbase

    fields = {}

    v = values_dict.get(value)
    if v is None: v = -999
    fields['health'] = v
    # fields['health'] = -999 if (v is None) else fields['health'] = v
    # if value == 'green':
    #     fields['health'] = 0

    # if value == 'yellow':
    #     fields['health'] = 1

    # if value == 'red':
    #     fields['health'] = 2

    # if value == 'skipped':
    #     fields['health'] = -1

    tags['health_title'] = value

    if value != 'skipped':
        printInfluxLineProtocol(measurement, tags, fields, timestamp)


def getCapacity(args, tagsbase):

    # Don't check for valid certificate
    context = ssl._create_unverified_context()

    si, _, cluster_obj = connectvCenter(args, context)

    # Disconnect to vcenter at the end
    atexit.register(Disconnect, si)

    apiVersion = vsanapiutils.GetLatestVmodlVersion(args.vcenter)
    vcMos = vsanapiutils.GetVsanVcMos(si._stub, context=context, version=apiVersion)

    vsanSpaceReportSystem = vcMos['vsan-cluster-space-report-system']

    try:
        spaceReport = vsanSpaceReportSystem.VsanQuerySpaceUsage(
            cluster=cluster_obj
        )
    except vmodl.fault.InvalidArgument as e:
        print("Caught InvalidArgument exception : " + str(e))
        return -1
    except vmodl.fault.NotSupported as e:
        print("Caught NotSupported exception : " + str(e))
        return -1

    except vmodl.fault.RuntimeFault as e:
        print("Caught RuntimeFault exception : " + str(e))
        return -1

    timestamp = int(time.time() * 1000000000)

    parseCapacity('global', spaceReport, tagsbase, timestamp)
    parseCapacity('summary', spaceReport, tagsbase, timestamp)

    if spaceReport.efficientCapacity:
        parseCapacity('efficientcapacity', spaceReport, tagsbase, timestamp)

    for object in spaceReport.spaceDetail.spaceUsageByObjectType:
        parseCapacity(object.objType, object, tagsbase, timestamp)


def getHealth(args, tagsbase):

    # Don't check for valid certificate
    context = ssl._create_unverified_context()

    si, _, cluster_obj = connectvCenter(args, context)

    # Disconnect to vcenter at the end
    atexit.register(Disconnect, si)

    apiVersion = vsanapiutils.GetLatestVmodlVersion(args.vcenter)
    vcMos = vsanapiutils.GetVsanVcMos(si._stub, context=context, version=apiVersion)
    vsanClusterHealthSystem = vcMos['vsan-cluster-health-system']

    try:
        clusterHealth = vsanClusterHealthSystem.VsanQueryVcClusterHealthSummary(
            cluster=cluster_obj
        )
    except vmodl.fault.NotFound as e:
        print("Caught NotFound exception : " + str(e))
        return -1
    except vmodl.fault.RuntimeFault as e:
        print("Caught RuntimeFault exception : " + str(e))
        return -1

    timestamp = int(time.time() * 1000000000)

    for group in clusterHealth.groups:

        splitGroupId = group.groupId.split('.')
        testName = splitGroupId[-1]

        parseHealth(testName, group.groupHealth, tagsbase, timestamp)


def getPerformanceTest(args, tagsbase):
    result = ""
    context = ssl._create_unverified_context()
    si, content, cluster_obj = connectvCenter(args, context)
    atexit.register(Disconnect, si)
    apiVersion = vsanapiutils.GetLatestVmodlVersion(args.vcenter)
    vcMos = vsanapiutils.GetVsanVcMos(si._stub, context=context, version=apiVersion)
    vsanVcStretchedClusterSystem = vcMos['vsan-stretched-cluster-system']
    vsanPerfSystem = vcMos['vsan-performance-manager']
    vms = getVMs(cluster_obj)
    uuid, disks = getInformations(content, cluster_obj)
    witnessHosts = vsanVcStretchedClusterSystem.VSANVcGetWitnessHosts(
        cluster=cluster_obj
    )
    for witnessHost in witnessHosts:
        host = (vim.HostSystem(witnessHost.host._moId, si._stub))
        uuid[witnessHost.nodeUuid] = host.name
        diskWitness = host.configManager.vsanSystem.QueryDisksForVsan()
        for disk in diskWitness:
            if disk.state == 'inUse':
                uuid[disk.vsanUuid] = disk.disk.canonicalName
                disks[disk.vsanUuid] = host.name
    # Gather a list of the available entity types (ex: vsan-host-net)
    entityTypes = vsanPerfSystem.VsanPerfGetSupportedEntityTypes()
    # query interval, last 10 minutes -- UTC !!!
    endTime = datetime.utcnow()
    startTime = endTime + timedelta(minutes=-10)
    splitSkipentitytypes = ['vsan-vnic-net', 'vsan-host-net', 'vsan-pnic-net', 'vsan-iscsi-host', 'vsan-iscsi-target', 'vsan-iscsi-lun']
    # if args.skipentiytypes == []:
    #     splitSkipentitytypes = args.skipentitytypes.split(',')
    for entities in entityTypes:
        if entities.name in splitSkipentitytypes: continue
        entitieName = entities.name
        entity = '%s:*' % (entitieName)
        print(entity)
        spec = vim.cluster.VsanPerfQuerySpec(
            entityRefId=entity,
            labels=constants.VSAN_SUPPORTED_ENTITIES.get(entitieName),
            startTime=startTime,
            endTime=endTime
        )
        try:
            metrics = vsanPerfSystem.VsanPerfQueryPerf(
                querySpecs=[spec],
                cluster=cluster_obj
            )
        except Exception as e:
            continue

        for metric in metrics:

            if not metric.sampleInfo == "":
                measurement = genMeasurementName('performance', entitieName)
                sampleInfos = metric.sampleInfo.split(",")
                lenValues = len(sampleInfos)

                timestamp = convertStrToTimestamp(sampleInfos[lenValues - 1])

                tags = parseEntityRefId(entitieName, metric.entityRefId, uuid, vms, disks)

                tags.update(tagsbase)

                fields = {}

                for value in metric.value:

                    listValue = value.values.split(",")

                    fields[value.metricId.label] = float(listValue[lenValues - 1])

                result = result + formatInfluxLineProtocol(measurement, tags, fields, timestamp)

    print(result)

def getPerformance(args, tagsbase):

    result = ""

    # Don't check for valid certificate
    context = ssl._create_unverified_context()

    si, content, cluster_obj = connectvCenter(args, context)

    # Disconnect to vcenter at the end
    atexit.register(Disconnect, si)

    apiVersion = vsanapiutils.GetLatestVmodlVersion(args.vcenter)
    vcMos = vsanapiutils.GetVsanVcMos(si._stub, context=context, version=apiVersion)

    vsanVcStretchedClusterSystem = vcMos['vsan-stretched-cluster-system']
    vsanPerfSystem = vcMos['vsan-performance-manager']

    # Get VM uuid/names
    vms = getVMs(cluster_obj)

    # Get uuid/names relationship informations for hosts and disks
    uuid, disks = getInformations(content, cluster_obj)

    # Witness
    # Retrieve Witness Host for given VSAN Cluster
    witnessHosts = vsanVcStretchedClusterSystem.VSANVcGetWitnessHosts(
        cluster=cluster_obj
    )

    for witnessHost in witnessHosts:
        host = (vim.HostSystem(witnessHost.host._moId, si._stub))

        uuid[witnessHost.nodeUuid] = host.name

        diskWitness = host.configManager.vsanSystem.QueryDisksForVsan()

        for disk in diskWitness:
            if disk.state == 'inUse':
                uuid[disk.vsanUuid] = disk.disk.canonicalName
                disks[disk.vsanUuid] = host.name

    # Gather a list of the available entity types (ex: vsan-host-net)
    entityTypes = vsanPerfSystem.VsanPerfGetSupportedEntityTypes()

    # query interval, last 10 minutes -- UTC !!!
    endTime = datetime.utcnow()
    startTime = endTime + timedelta(minutes=-10)

    splitSkipentitytypes = []

    if args.skipentitytypes:
            splitSkipentitytypes = args.skipentitytypes.split(',')

    # labels=['iopsRead', 'iopsWrite', 'latencyAvgRead', 'latencyAvgWrite', 'congestion', 'oio', 'throughputRead', 'throughputWrite', 'readCount']
    # labels=['readCount']
    # mmetrics = []
    # while len(mmetrics) == 0:
    #     if len(labels) == 0: break
    #     try:
    #         spec = vim.cluster.VsanPerfQuerySpec(
    #             entityRefId='cluster-domclient:*',
    #             labels=labels,
    #             startTime=startTime,
    #             endTime=endTime
    #         )
    #         mmetrics = vsanPerfSystem.VsanPerfQueryPerf(
    #             querySpecs=[spec],
    #             cluster=cluster_obj
    #         )
    #     except vmodl.fault.SystemError as e:
    #         col = getattr(e, 'msg', repr(e)).split(': ')[1]
    #         labels.remove(col)
    #         print(col)

    # print("Metrics")
    # print(mmetrics)
    # for entities in entityTypes:
    #     print(entities.name)
    # return -1
    for entities in entityTypes:

        if entities.name not in splitSkipentitytypes:

            entitieName = entities.name

            labels = []

            # Gather all labels related to the entity (ex: iopsread, iopswrite...)
            for entity in entities.graphs:

                for metric in entity.metrics:

                        labels.append(metric.label)

            # Build entity
            entity = '%s:*' % (entities.name)

            metrics = []
            while len(metrics) == 0:
                if len(labels) == 0: break
                try:
                    # Build spec object
                    spec = vim.cluster.VsanPerfQuerySpec(
                        endTime=endTime,
                        entityRefId=entity,
                        labels=labels,
                        startTime=startTime
                    )
                    metrics = vsanPerfSystem.VsanPerfQueryPerf(
                        querySpecs=[spec],
                        cluster=cluster_obj
                    )
                except vmodl.fault.SystemError as e:
                    msg = getattr(e, 'msg', repr(e))
                    labels.remove(msg.split(': ')[1])
                except vmodl.fault.InvalidArgument as e:
                    break
            # except vmodl.fault.InvalidArgument as e:
            #     print("Caught InvalidArgument exception : " + str(e))
            #     return -1

            # except vmodl.fault.NotFound as e:
            #     print("Caught NotFound exception : " + str(e))
            #     return -1

            # except vmodl.fault.NotSupported as e:
            #     print("Caught NotSupported exception : " + str(e))
            #     return -1

            # except vmodl.fault.RuntimeFault as e:
            #     print("Caught RuntimeFault exception : " + str(e))
            #     return -1

            # except vmodl.fault.Timedout as e:
            #     print("Caught Timedout exception : " + str(e))
            #     return -1

            # except vmodl.fault.VsanNodeNotMaster as e:
            #     print("Caught VsanNodeNotMaster exception : " + str(e))
            #     return -1

            for metric in metrics:

                if not metric.sampleInfo == "":
                    measurement = genMeasurementName('performance', entitieName)
                    sampleInfos = metric.sampleInfo.split(",")
                    lenValues = len(sampleInfos)

                    timestamp = convertStrToTimestamp(sampleInfos[lenValues - 1])

                    tags = parseEntityRefId(entitieName, metric.entityRefId, uuid, vms, disks)

                    tags.update(tagsbase)

                    fields = {}

                    for value in metric.value:

                        listValue = value.values.split(",")

                        fields[value.metricId.label] = float(listValue[lenValues - 1])

                    result = result + formatInfluxLineProtocol(measurement, tags, fields, timestamp)

    print(result)


def connectvCenter(args, context):

    # Connect to vCenter
    try:
        si = SmartConnect(host=args.vcenter,
                          user=args.user,
                          pwd=args.password,
                          port=int(args.port),
                          sslContext=context)
        if not si:
            print("Could not connect to the specified host using specified "
                  "username and password")

            return -1
    except vmodl.MethodFault as e:
        print("Caught vmodl fault : " + e.msg)
        return -1

    except Exception as e:
        print("Caught exception : " + str(e))
        return -1

    # Get content informations
    content = si.RetrieveContent()

    # Get Info about cluster
    cluster_obj = getClusterInstance(args.clusterName, content)

    # Exit if the cluster provided in the arguments is not available
    if not cluster_obj:
        print 'The required cluster not found in inventory, validate input.'
        return -1

    return si, content, cluster_obj

def removeChar(string, pattern):
    str = list(string)
    str.remove(pattern)
    return ''.join(str)


def parseVMDetailedInfo(vmIdentity, host, tagsbase, timestamp):
    tags = {}
    fields = { 'physicalUsedGB': 0.0, 'reservedCapacityGB': 0.0 }
    tags.update(tagsbase)
    tags['esxhost'] = host.name
    vis = host.configManager.vsanInternalSystem
    for identity in vmIdentity['objIdentities']:
        if identity['type'] == 'namespace':
            attrs = vis.GetVsanObjExtAttrs(identity['uuid'])
            json_attrs = json.loads(attrs)
            tags['vmname'] = json_attrs.get(identity['uuid']).get('User friendly name')
        elif identity['type'] == 'vdisk':
            physical = identity.get('physicalUsedB')
            reserved = identity.get('reservedCapacityB')
            if physical != None and reserved != None:
                fields['physicalUsedGB'] += float(physical)/float(1024*1024)
                fields['reservedCapacityGB'] += float(reserved)/float(1024*1024)
    return formatInfluxLineProtocol('prevensys_vsan_vm_detailed_info', tags, fields, timestamp)

def getDiskInfo(args, tagsbase):
    result = ""
    context = ssl._create_unverified_context()
    si, _, cluster_obj = connectvCenter(args, context)
    atexit.register(Disconnect, si)
    apiVersion = vsanapiutils.GetLatestVmodlVersion(args.vcenter)
    vms = getVMs(cluster_obj)
    timestamp = int(time.time() * 1000000000)

    for host in cluster_obj.host:
        try:
            print(host.name)
            host_si = SmartConnect(host=host.name,user=args.esxUsername, pwd=args.esxPassword,port=443, sslContext=context)
            vsanStub = vsanapiutils.GetVsanEsxStub(host_si._stub, context)
            esxvc = vim.cluster.VsanObjectSystem('vsan-object-system', vsanStub)
            results = esxvc.VsanQueryObjectIdentities(includeObjIdentity=True, includeSpaceSummary=True)
            vis = host.configManager.vsanInternalSystem
            jsonResults = json.loads(results.rawData)
            vmIdentities = jsonResults['identities']['vmIdentities']
            for vmIdentity in vmIdentities:
                try:
                    result += parseVMDetailedInfo(vmIdentity, host, tagsbase, timestamp)
                except Exception as e:
                    print("Caught exception in VMS disks " + str(e))
                    return -1

        except Exception as e:
            print("Caught exception in VMS disks" + str(e))
            return -1
        finally:
            Disconnect(host_si)

        # print(host.name)
        # host_si = SmartConnect(host=host.name,user=args.esxUsername, pwd=args.esxPassword,port=443, sslContext=context)
        # vsanStub = vsanapiutils.GetVsanEsxStub(host_si._stub, context)
        # esxvc = vim.cluster.VsanObjectSystem('vsan-object-system', vsanStub)
        # results = esxvc.VsanQueryObjectIdentities(includeObjIdentity=True, includeSpaceSummary=True)
        # vis = host.configManager.vsanInternalSystem
        # jsonResults = json.loads(results.rawData)
        # vmIdentities = jsonResults['identities']['vmIdentities']
        # for vmIdentity in vmIdentities:
        #     try:
        #         result += parseVMDetailedInfo(vmIdentity, host, tagsbase, timestamp)
        #     except Exception as e:
        #         print("Caught exception in VMS disks " + str(e))
        #         return -1
        # Disconnect(host_si)

    print(result)

# Main...
def main():

    # Parse CLI arguments
    args = get_args()

    # Initiate tags with vcenter and cluster name

    # print("Cluster: %s", args.clusterName)
    tagsbase = {}
    tagsbase['vcenter'] = args.vcenter
    tagsbase['cluster'] = args.clusterName

    # CAPACITY
    if args.capacity:
        Process(target=getCapacity, args=(args, tagsbase,)).start()

    # HEALTH
    if args.health:
        Process(target=getHealth, args=(args, tagsbase,)).start()

    # PERFORMANCE
    if args.performance:
        Process(target=getPerformance, args=(args, tagsbase,)).start()

    if args.disks:
        Process(target=getDiskInfo, args=(args, tagsbase,)).start()

    return 0

# Start program
if __name__ == "__main__":
    main()
