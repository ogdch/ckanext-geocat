# -*- coding: utf-8 -*-

import requests
import traceback

from ckan.lib.helpers import json
from ckan.lib.munge import munge_tag
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.harvesters import HarvesterBase
import ckanext.geocat.metadata as md 
from ckan.logic import get_action, NotFound
from ckan import model
from ckan.model import Session, Package, PACKAGE_NAME_MAX_LENGTH

from pylons import config

import logging
log = logging.getLogger(__name__)


class GeocatHarvester(HarvesterBase):
    '''
    The harvester for geocat
    '''

    HARVEST_USER = 'harvest'

    def info(self):
        return {
            'name': 'geocat_harvester',
            'title': 'Geocat harvester',
            'description': (
                'Harvests metadata from geocat (CSW)'
            ),
            'form_config_interface': 'Text'
        }

    def _set_config(self, config_str):
        if config_str:
            self.config = json.loads(config_str)
        else:
            self.config = {}

        if 'user' not in self.config:
            self.config['user'] = self.HARVEST_USER

        log.debug('Using config: %r' % self.config)

    def _find_existing_package(self, package_dict):
        data_dict = {'identifier': package_dict['identifier']}
        package_show_context = {'model': model, 'session': Session,
                                'ignore_auth': True}
        return get_action('ogdch_dataset_by_identifier')(
            package_show_context, data_dict)

    def gather_stage(self, harvest_job):
        log.debug('In GeocatHarvester gather_stage')

        try:
            self._set_config(harvest_job.source.config)
        except GeocatConfigError, e:
            self._save_gather_error(
                'Config value missing: %s' % str(e),
                harvest_job
            )
            return False

        csw_url = None
        try:
            harvest_obj_ids = []
            csw_url = harvest_job.source.url.rstrip('/')
            csw = md.CswHelper(url=csw_url)

            cql = self.config.get('cql', None)
            if cql is None:
                cql = "csw:AnyText Like '%Bahnhof%'"
                
            log.debug("CQL query: %s" % cql)
            for record_id in csw.get_id_by_search(cql=cql):
                harvest_obj = HarvestObject(
                    guid=record_id,
                    job=harvest_job
                )
                harvest_obj.save()
                harvest_obj_ids.append(harvest_obj.id)

            log.debug('IDs: %r' % harvest_obj_ids)

            return harvest_obj_ids
        except Exception, e:
            self._save_gather_error(
                'Unable to get content for URL: %s: %s / %s'
                % (csw_url, str(e), traceback.format_exc()),
                harvest_job
            )

    def fetch_stage(self, harvest_object):
        log.debug('In GeocatHarvester fetch_stage')
        self._set_config(harvest_object.job.source.config)

        if not harvest_object:
            log.error('No harvest object received')
            self._save_object_error(
                'No harvest object received',
                harvest_object
            )
            return False

        csw_url = harvest_object.source.url.rstrip('/')
        try:
            csw = md.CswHelper(url=csw_url)
            xml = csw.get_by_id(harvest_object.guid)
            harvest_object.content = xml
            harvest_object.save()
            log.debug('successfully processed ' + harvest_object.guid)
            return True
        except Exception, e:
            self._save_object_error(
                (
                    'Unable to get content for package: %s: %r / %s'
                    % (harvest_object.guid, e, traceback.format_exc())
                ),
                harvest_object
            )
            return False

    def import_stage(self, harvest_object):
        log.debug('In GeocatHarvester import_stage')
        self._set_config(harvest_object.job.source.config)

        if not harvest_object:
            log.error('No harvest object received')
            self._save_object_error(
                'No harvest object received',
                harvest_object
            )
            return False

        try:

            if 'organization' not in self.config:
                context = {
                    'model': model,
                    'session': Session,
                    'ignore_auth': True
                }
                source_dataset = get_action('package_show')(context, {'id': harvest_object.source.id})
                self.config['organization'] = source_dataset.get('organization').get('name')

            dataset_metadata = md.GeocatDcatDatasetMetadata()
            dist_metadata = md.GeocatDcatDistributionMetadata()

            pkg_dict = dataset_metadata.get_metadata(harvest_object.content)
            dist_list = dist_metadata.get_metadata(harvest_object.content)

            for dist in dist_list:
                dist['rights'] = self.config.get('rights', 'NonCommercialNotAllowed-CommercialNotAllowed-ReferenceRequired')

            pkg_dict['identifier'] = '%s@%s' % (pkg_dict['identifier'], self.config['organization'])
            pkg_dict['owner_org'] = self.config['organization']
            pkg_dict['resources'] = dist_list
            pkg_dict['relations'] = []
            pkg_dict['see_alsos'] = []
            pkg_dict['temporals'] = []
            try:
                pkg_dict['language'] = []
                pkg_dict['language'] = dist_list[0]['language']
            except IndexError:
                pass

            log.debug('package dict: %s' % pkg_dict)

            if 'id' not in pkg_dict:
                pkg_dict['id'] = ''
            pkg_dict['name'] = self._gen_new_name(pkg_dict['title']['de'])

            log.debug("New Name: %s" % pkg_dict['name'])

            #return self._create_or_update_package(pkg_dict, harvest_object)
            package_context = {'ignore_auth': True}
            try:
                existing = self._find_existing_package(pkg_dict)
                log.debug("Existing package found, updating %s..." % existing['id'])
                pkg_dict['name'] = existing['name']
                pkg_dict['id'] = existing['id']
                updated_pkg = get_action('package_update')(package_context, pkg_dict)
                log.debug("Updated PKG: %s" % updated_pkg)

                # Flag the other objects linking to this package as not current anymore
                from ckanext.harvest.model import harvest_object_table
                conn = Session.connection()
                u = update(harvest_object_table) \
                        .where(harvest_object_table.c.package_id==bindparam('b_package_id')) \
                        .values(current=False)
                conn.execute(u, b_package_id=pkg_dict['id'])

                # Flag this as the current harvest object
                harvest_object.package_id = pkg_dict['id']
                harvest_object.current = True
                harvest_object.save()
            except NotFound:
                log.debug("No package found, create a new one!")

                harvest_object.current = True
                model.Session.execute('SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
                model.Session.flush()

                created_pkg = get_action('package_create')(package_context, pkg_dict)

                harvest_object.package_id = created_pkg['id']
                harvest_object.add()

                log.debug("Created PKG: %s" % created_pkg)

            Session.commit()
            return True

        except Exception, e:
            self._save_object_error(
                (
                    'Exception in import stage: %r / %s'
                    % (e, traceback.format_exc())
                ),
                harvest_object
            )
            return False

class GeocatConfigError(Exception):
    pass
