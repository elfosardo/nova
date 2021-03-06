#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os_traits
from oslo_log import log as logging

import nova.conf
from nova import exception
from nova.i18n import _
from nova import objects


CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
TENANT_METADATA_KEY = 'filter_tenant_id'


def require_tenant_aggregate(ctxt, request_spec):
    """Require hosts in an aggregate based on tenant id.

    This will modify request_spec to request hosts in an aggregate
    defined specifically for the tenant making the request. We do that
    by looking for a nova host aggregate with metadata indicating which
    tenant it is for, and passing that aggregate uuid to placement to
    limit results accordingly.
    """

    enabled = CONF.scheduler.limit_tenants_to_placement_aggregate
    agg_required = CONF.scheduler.placement_aggregate_required_for_tenants
    if not enabled:
        return

    aggregates = objects.AggregateList.get_by_metadata(
        ctxt, value=request_spec.project_id)
    aggregate_uuids_for_tenant = set([])
    for agg in aggregates:
        for key, value in agg.metadata.items():
            if key.startswith(TENANT_METADATA_KEY):
                aggregate_uuids_for_tenant.add(agg.uuid)
                break

    if aggregate_uuids_for_tenant:
        if ('requested_destination' not in request_spec or
                request_spec.requested_destination is None):
            request_spec.requested_destination = objects.Destination()
        destination = request_spec.requested_destination
        destination.require_aggregates(aggregate_uuids_for_tenant)
    elif agg_required:
        LOG.warning('Tenant %(tenant)s has no available aggregates',
                    {'tenant': request_spec.project_id})
        raise exception.RequestFilterFailed(
            reason=_('No hosts available for tenant'))


def map_az_to_placement_aggregate(ctxt, request_spec):
    """Map requested nova availability zones to placement aggregates.

    This will modify request_spec to request hosts in an aggregate that
    matches the desired AZ of the user's request.
    """
    if not CONF.scheduler.query_placement_for_availability_zone:
        return

    az_hint = request_spec.availability_zone
    if not az_hint:
        return

    aggregates = objects.AggregateList.get_by_metadata(ctxt,
                                                       key='availability_zone',
                                                       value=az_hint)
    if aggregates:
        if ('requested_destination' not in request_spec or
                request_spec.requested_destination is None):
            request_spec.requested_destination = objects.Destination()
        request_spec.requested_destination.require_aggregates(
            [agg.uuid for agg in aggregates])


def require_image_type_support(ctxt, request_spec):
    """Request type-specific trait on candidates.

    This will modify the request_spec to request hosts that support the
    disk_format of the image provided.
    """
    if not CONF.scheduler.query_placement_for_image_type_support:
        return

    if request_spec.is_bfv:
        # We are booting from volume, and thus compute node image
        # disk_format support does not matter.
        return

    disk_format = request_spec.image.disk_format
    trait_name = 'COMPUTE_IMAGE_TYPE_%s' % disk_format.upper()
    if not hasattr(os_traits, trait_name):
        LOG.error(('Computed trait name %r is not valid; '
                   'is os-traits up to date?'), trait_name)
        return

    # NOTE(danms): We are using the transient flavor in the request spec
    # to add the trait that we need. We make sure that we reset the dirty-ness
    # of this field to avoid persisting it.
    request_spec.flavor.extra_specs['trait:%s' % trait_name] = 'required'
    request_spec.obj_reset_changes(fields=['flavor'], recursive=True)


ALL_REQUEST_FILTERS = [
    require_tenant_aggregate,
    map_az_to_placement_aggregate,
    require_image_type_support,
]


def process_reqspec(ctxt, request_spec):
    """Process an objects.ReqestSpec before calling placement.

    :param ctxt: A RequestContext
    :param request_spec: An objects.RequestSpec to be inspected/modified
    """
    for filter in ALL_REQUEST_FILTERS:
        filter(ctxt, request_spec)
