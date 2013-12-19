#!/usr/bin/env python
"""
This script cannot be run directly, because it needs to have test/functional/test_toolbox.py in sys.argv in
order to run functional tests on repository tools after installation. The install_and_test_tool_shed_repositories.sh
will execute this script with the appropriate parameters.
"""
import os
import sys
# Assume we are run from the galaxy root directory, add lib to the python path
cwd = os.getcwd()
sys.path.append( cwd )
new_path = [ os.path.join( cwd, "scripts" ),
             os.path.join( cwd, "lib" ),
             os.path.join( cwd, 'test' ),
             os.path.join( cwd, 'scripts', 'api' ) ]
new_path.extend( sys.path )
sys.path = new_path

from galaxy import eggs
eggs.require( "nose" )
eggs.require( "Paste" )
eggs.require( 'mercurial' )
# This should not be required, but it is under certain conditions thanks to this bug:
# http://code.google.com/p/python-nose/issues/detail?id=284
eggs.require( "pysqlite" )

import httplib
import install_and_test_tool_shed_repositories.base.test_db_util as test_db_util
import install_and_test_tool_shed_repositories.functional.test_install_repositories as test_install_repositories
import logging
import nose
import random
import re
import shutil
import socket
import string
import tempfile
import time
import threading

import install_and_test_tool_shed_repositories.base.util as install_and_test_base_util

from base.tool_shed_util import parse_tool_panel_config

from galaxy.app import UniverseApplication
from galaxy.util.json import from_json_string
from galaxy.util.json import to_json_string
from galaxy.util import unicodify
from galaxy.web import buildapp
from functional_tests import generate_config_file
from nose.plugins import Plugin
from paste import httpserver

from functional import database_contexts

log = logging.getLogger( 'install_and_test_repositories_with_tools' )

assert sys.version_info[ :2 ] >= ( 2, 6 )
test_home_directory = os.path.join( cwd, 'test', 'install_and_test_tool_shed_repositories', 'repositories_with_tools' )

test_toolbox = None

# Here's the directory where everything happens.  Temporary directories are created within this directory to contain
# the database, new repositories, etc.
galaxy_test_tmp_dir = os.path.join( test_home_directory, 'tmp' )
default_galaxy_locales = 'en'
default_galaxy_test_file_dir = "test-data"
os.environ[ 'GALAXY_INSTALL_TEST_TMP_DIR' ] = galaxy_test_tmp_dir
# This file is copied to the Galaxy root directory by buildbot. 
# It is managed by cfengine and is not locally available.
exclude_list_file = os.environ.get( 'GALAXY_INSTALL_TEST_EXCLUDE_REPOSITORIES', 'repositories_with_tools_exclude.xml' )

# This script can be run in such a way that no Tool Shed database records should be changed.
if '-info_only' in sys.argv or 'GALAXY_INSTALL_TEST_INFO_ONLY' in os.environ:
    can_update_tool_shed = False
else:
    can_update_tool_shed = True

test_framework = install_and_test_base_util.REPOSITORIES_WITH_TOOLS

def get_failed_test_dicts( test_result, from_tool_test=True ):
    """Extract any useful data from the test_result.failures and test_result.errors attributes."""
    failed_test_dicts = []
    for failure in test_result.failures + test_result.errors:
        # Record the twill test identifier and information about the tool so the repository owner
        # can discover which test is failing.
        test_id = str( failure[ 0 ] )
        if not from_tool_test:
            tool_id = None
            tool_version = None
        else:
            tool_id, tool_version = get_tool_info_from_test_id( test_id )
        test_status_dict = dict( test_id=test_id, tool_id=tool_id, tool_version=tool_version )
        log_output = failure[ 1 ].replace( '\\n', '\n' )
        # Remove debug output.
        log_output = re.sub( r'control \d+:.+', r'', log_output )
        log_output = re.sub( r'\n+', r'\n', log_output )
        appending_to = 'output'
        tmp_output = {}
        output = {}
        # Iterate through the functional test output and extract only the important data. Captured
        # logging and stdout are not recorded.
        for line in log_output.split( '\n' ):
            if line.startswith( 'Traceback' ):
                appending_to = 'traceback'
            elif '>> end captured' in line or '>> end tool' in line:
                continue
            elif 'request returned None from get_history' in line:
                continue
            elif '>> begin captured logging <<' in line:
                appending_to = 'logging'
                continue
            elif '>> begin captured stdout <<' in line:
                appending_to = 'stdout'
                continue
            elif '>> begin captured stderr <<' in line or '>> begin tool stderr <<' in line:
                appending_to = 'stderr'
                continue
            if appending_to in tmp_output:
                tmp_output[ appending_to ].append( line )
            else:
                tmp_output[ appending_to ] = [ line ]
        for output_type in [ 'stderr', 'traceback' ]:
            if output_type in tmp_output:
                test_status_dict[ output_type ] = '\n'.join( tmp_output[ output_type ] )
        failed_test_dicts.append( test_status_dict )
    return failed_test_dicts

def get_tool_info_from_test_id( test_id ):
    """
    Test IDs come in the form test_tool_number
    (functional.test_toolbox.TestForTool_toolshed_url/repos/owner/repository_name/tool_id/tool_version)
    We want the tool ID and tool version.
    """
    parts = test_id.replace( ')', '' ).split( '/' )
    tool_version = parts[ -1 ]
    tool_id = parts[ -2 ]
    return tool_id, tool_version

def install_and_test_repositories( app, galaxy_shed_tools_dict, galaxy_shed_tool_conf_file ):
    install_and_test_statistics_dict = install_and_test_base_util.initialize_install_and_test_statistics_dict( test_framework )
    error_message = ''
    # Initialize a dictionary for the summary that will be printed to stdout.
    total_repositories_processed = install_and_test_statistics_dict[ 'total_repositories_processed' ]
    repositories_to_install, error_message = \
        install_and_test_base_util.get_repositories_to_install( install_and_test_base_util.galaxy_tool_shed_url, test_framework )
    if error_message:
        return None, error_message
    # Handle repositories not to be tested.
    if os.path.exists( exclude_list_file ):
        # Entries in the exclude_list look something like this.
        # { 'reason': The default reason or the reason specified in this section,
        #   'repositories':
        #         [( name, owner, changeset revision if changeset revision else None ),
        #          ( name, owner, changeset revision if changeset revision else None )] }
        # If changeset revision is None, that means the entire repository is excluded from testing, otherwise only the specified
        # revision should be skipped.
        # We are testing deprecated repositories because it is possible that a deprecated repository contains valid tools that
        # someone has previously installed. Deleted repositories have never been installed, so should not be tested. If they are
        # undeleted, this script will then test them the next time it runs. We don't need to check if a repository has been deleted
        # here because our call to the Tool Shed API filters by downloadable='true', in which case deleted will always be False.
        log.debug( 'Loading the list of repositories excluded from testing from the file %s...' % \
            str( exclude_list_file ) )
        exclude_list = install_and_test_base_util.parse_exclude_list( exclude_list_file )
    else:
        exclude_list = []
    # Generate a test method that will use Twill to install each repository into the embedded Galaxy application that was
    # started up, installing repository and tool dependencies. Upon successful installation, generate a test case for each
    # functional test defined for each tool in the repository and execute the test cases. Record the result of the tests.
    # The traceback and captured output of the tool that was run will be recored for test failures.  After all tests have
    # completed, the repository is uninstalled, so test cases don't interfere with the next repository's functional tests.
    for repository_dict in repositories_to_install:
        # Each repository_dict looks something like:
        # { "changeset_revision": "13fa22a258b5",
        #   "contents_url": "/api/repositories/529fd61ab1c6cc36/contents",
        #   "deleted": false,
        #   "deprecated": false,
        #   "description": "Convert column case.",
        #   "downloadable": true,
        #   "id": "529fd61ab1c6cc36",
        #   "long_description": "This tool takes the specified columns and converts them to uppercase or lowercase.",
        #   "malicious": false,
        #   "name": "change_case",
        #   "owner": "test",
        #   "private": false,
        #   "repository_id": "529fd61ab1c6cc36",
        #   "times_downloaded": 0,
        #   "tool_shed_url": "http://toolshed.local:10001",
        #   "url": "/api/repository_revisions/529fd61ab1c6cc36",
        #   "user_id": "529fd61ab1c6cc36" }
        encoded_repository_metadata_id = repository_dict.get( 'id', None )
        # Add the URL for the tool shed we're installing from, so the automated installation methods go to the right place.
        repository_dict[ 'tool_shed_url' ] = install_and_test_base_util.galaxy_tool_shed_url
        # Get the name and owner out of the repository info dict.
        name = str( repository_dict[ 'name' ] )
        owner = str( repository_dict[ 'owner' ] )
        changeset_revision = str( repository_dict[ 'changeset_revision' ] )
        log.debug( "Processing revision %s of repository %s owned by %s..." % ( changeset_revision, name, owner ) )
        repository_identifier_dict = dict( name=name, owner=owner, changeset_revision=changeset_revision )
        # Retrieve the stored list of tool_test_results_dicts.
        tool_test_results_dicts, error_message = \
            install_and_test_base_util.get_tool_test_results_dicts( install_and_test_base_util.galaxy_tool_shed_url,
                                                                    encoded_repository_metadata_id )
        if error_message:
            log.debug( error_message )
        else:
            tool_test_results_dict = install_and_test_base_util.get_tool_test_results_dict( tool_test_results_dicts )
            # See if this repository should be skipped for any reason.
            this_repository_is_in_the_exclude_list = False
            requires_excluded = False
            skip_reason = None
            for exclude_dict in exclude_list:
                reason = exclude_dict[ 'reason' ]
                exclude_repositories = exclude_dict[ 'repositories' ]
                # 'repositories':
                #    [( name, owner, changeset_revision if changeset_revision else None ),
                #     ( name, owner, changeset_revision if changeset_revision else None )]
                if ( name, owner, changeset_revision ) in exclude_repositories or ( name, owner, None ) in exclude_repositories:
                    this_repository_is_in_the_exclude_list = True
                    skip_reason = reason
                    break
                if not this_repository_is_in_the_exclude_list:
                    # Skip this repository if it has a repository dependency that is in the exclude list.
                    repository_dependency_dicts, error_message = \
                        install_and_test_base_util.get_repository_dependencies_for_changeset_revision( install_and_test_base_util.galaxy_tool_shed_url,
                                                                                                       encoded_repository_metadata_id )
                    if error_message:
                        log.debug( 'Error getting repository dependencies for revision %s of repository %s owned by %s:' % \
                            ( changeset_revision, name, owner ) )
                        log.debug( error_message )
                    else:
                        for repository_dependency_dict in repository_dependency_dicts:
                            rd_name = repository_dependency_dict[ 'name' ]
                            rd_owner = repository_dependency_dict[ 'owner' ]
                            rd_changeset_revision = repository_dependency_dict[ 'changeset_revision' ]
                            if ( rd_name, rd_owner, rd_changeset_revision ) in exclude_repositories or \
                                ( rd_name, rd_owner, None ) in exclude_repositories:
                                skip_reason = 'This repository requires revision %s of repository %s owned by %s which is excluded from testing.' % \
                                    ( rd_changeset_revision, rd_name, rd_owner )
                                requires_excluded = True
                                break
            if this_repository_is_in_the_exclude_list or requires_excluded:
                # If this repository is being skipped, register the reason.
                tool_test_results_dict[ 'not_tested' ] = dict( reason=skip_reason )
                params = dict( do_not_test=False )
                # TODO: do something useful with response_dict
                response_dict = install_and_test_base_util.register_test_result( install_and_test_base_util.galaxy_tool_shed_url,
                                                                                 tool_test_results_dicts,
                                                                                 tool_test_results_dict,
                                                                                 repository_dict,
                                                                                 params,
                                                                                 can_update_tool_shed )
                log.debug( "Not testing revision %s of repository %s owned by %s because it is in the exclude list for this test run." % \
                    ( changeset_revision, name, owner ) )
            else:
                tool_test_results_dict = install_and_test_base_util.initialize_tool_tests_results_dict( app, tool_test_results_dict )
                # Explicitly clear tests from twill's test environment.
                remove_generated_tests( app )
                # Proceed with installing repositories and testing contained tools.
                repository, error_message = install_and_test_base_util.install_repository( app, repository_dict )
                install_and_test_statistics_dict[ 'total_repositories_processed' ] += 1
                if error_message:
                    # The repository installation failed.
                    log.debug( 'Installation failed for revision %s of repository %s owned by %s.' % \
                        ( changeset_revision, name, owner ) )
                    install_and_test_statistics_dict[ 'repositories_with_installation_error' ].append( repository_identifier_dict )
                    tool_test_results_dict[ 'installation_errors' ][ 'current_repository' ] = error_message
                    # Even if the repository failed to install, execute the uninstall method, in case a dependency did succeed.
                    log.debug( 'Attempting to uninstall revision %s of repository %s owned by %s.' % ( changeset_revision, name, owner ) )
                    try:
                        repository = \
                            test_db_util.get_installed_repository_by_name_owner_changeset_revision( name, owner, changeset_revision )
                    except Exception, e:
                        error_message = 'Unable to find revision %s of repository %s owned by %s: %s.' % \
                            ( changeset_revision, name, owner, str( e ) )
                        log.exception( error_message )
                    params = dict( test_install_error=True,
                                   do_not_test=False )
                    # TODO: do something useful with response_dict
                    response_dict = install_and_test_base_util.register_test_result( install_and_test_base_util.galaxy_tool_shed_url,
                                                                                     tool_test_results_dicts,
                                                                                     tool_test_results_dict,
                                                                                     repository_dict,
                                                                                     params,
                                                                                     can_update_tool_shed )
                    try:
                        # We are uninstalling this repository and all of its repository dependencies.
                        install_and_test_base_util.uninstall_repository_and_repository_dependencies( app, repository_dict )
                    except Exception, e:
                        log.exception( 'Error attempting to uninstall revision %s of repository %s owned by %s: %s' % \
                            ( changeset_revision, name, owner, str( e ) ) )
                    # Clean out any generated tests. This is necessary for Twill.
                    remove_generated_tests( app )
                    install_and_test_statistics_dict[ 'repositories_with_installation_error' ].append( repository_identifier_dict )
                    log.debug( 'Installation succeeded for revision %s of repository %s owned by %s.' % \
                        ( changeset_revision, name, owner ) )
                else:
                    # The repository was successfully installed.
                    params, install_and_test_statistics_dict, tool_test_results_dict = \
                        install_and_test_base_util.register_installed_and_missing_dependencies( app,
                                                                                                repository,
                                                                                                repository_identifier_dict,
                                                                                                install_and_test_statistics_dict,
                                                                                                tool_test_results_dict )
                    # Add an empty 'missing_test_results' entry if it is missing from the tool_test_results_dict.  The 
                    # ~/tool_shed/scripts/check_repositories_for_functional_tests.py will have entered information in the
                    # 'missing_test_components' entry of the tool_test_results_dict dictionary for repositories that are
                    # missing test components.
                    if 'missing_test_components' not in tool_test_results_dict:
                        tool_test_results_dict[ 'missing_test_components' ] = []
                    missing_tool_dependencies = install_and_test_base_util.get_missing_tool_dependencies( repository )
                    if missing_tool_dependencies or repository.missing_repository_dependencies:                        
                        install_and_test_statistics_dict = \
                            install_and_test_base_util.handle_missing_dependencies( app=app,
                                                                                    repository=repository,
                                                                                    missing_tool_dependencies=missing_tool_dependencies,
                                                                                    repository_dict=repository_dict,
                                                                                    tool_test_results_dicts=tool_test_results_dicts,
                                                                                    tool_test_results_dict=tool_test_results_dict,
                                                                                    params=params,
                                                                                    can_update_tool_shed=can_update_tool_shed )
                        # Set the test_toolbox.toolbox module-level variable to the new app.toolbox.
                        test_toolbox.toolbox = app.toolbox
                    else:
                        # This repository and all of its dependencies were successfully installed.
                        # Configure and run functional tests for this repository. This is equivalent to
                        # sh run_functional_tests.sh -installed
                        remove_install_tests()
                        log.debug( 'Installation of %s succeeded, running all defined functional tests.' % str( repository.name ) )
                        # Generate the shed_tools_dict that specifies the location of test data contained within this repository. If the repository
                        # does not have a test-data directory, this will return has_test_data = False, and we will set the do_not_test flag to True,
                        # and the tools_functionally_correct flag to False, as well as updating tool_test_results.
                        file( galaxy_shed_tools_dict, 'w' ).write( to_json_string( {} ) )
                        has_test_data, shed_tools_dict = \
                            parse_tool_panel_config( galaxy_shed_tool_conf_file,
                                                     from_json_string( file( galaxy_shed_tools_dict, 'r' ).read() ) )
                        # If the repository has a test-data directory we write the generated shed_tools_dict to a file, so the functional
                        # test framework can find it.
                        file( galaxy_shed_tools_dict, 'w' ).write( to_json_string( shed_tools_dict ) )
                        log.debug( 'Saved generated shed_tools_dict to %s\nContents: %s' % ( str( galaxy_shed_tools_dict ),
                                                                                             str( shed_tools_dict ) ) )
                        try:
                            install_and_test_statistics_dict = test_repository_tools( app,
                                                                                      repository,
                                                                                      repository_dict,
                                                                                      tool_test_results_dicts,
                                                                                      tool_test_results_dict,
                                                                                      install_and_test_statistics_dict )
                        except Exception, e:
                            exception_message = 'Error executing tests for repository %s: %s' % ( name, str( e ) )
                            log.exception( exception_message )
                            tool_test_results_dict[ 'failed_tests' ].append( exception_message )
                            install_and_test_statistics_dict[ 'at_least_one_test_failed' ].append( repository_identifier_dict )
                            # Record the status of this repository in the tool shed.
                            params[ 'tools_functionally_correct' ] = False
                            # TODO: do something useful with response_dict
                            response_dict = \
                                install_and_test_base_util.register_test_result( install_and_test_base_util.galaxy_tool_shed_url,
                                                                                 tool_test_results_dicts,
                                                                                 tool_test_results_dict,
                                                                                 repository_dict,
                                                                                 params,
                                                                                 can_update_tool_shed )
    return install_and_test_statistics_dict, error_message

def main():
    if install_and_test_base_util.tool_shed_api_key is None:
        # If the tool shed URL specified in any dict is not present in the tool_sheds_conf.xml, the installation will fail.
        log.debug( 'Cannot proceed without a valid tool shed API key set in the enviroment variable GALAXY_INSTALL_TEST_TOOL_SHED_API_KEY.' )
        return 1
    if install_and_test_base_util.galaxy_tool_shed_url is None:
        log.debug( 'Cannot proceed without a valid Tool Shed base URL set in the environment variable GALAXY_INSTALL_TEST_TOOL_SHED_URL.' )
        return 1
    # ---- Configuration ------------------------------------------------------
    galaxy_test_host = os.environ.get( 'GALAXY_INSTALL_TEST_HOST', install_and_test_base_util.default_galaxy_test_host )
    # Set the GALAXY_INSTALL_TEST_HOST variable so that Twill will have the Galaxy url to which to
    # install repositories.
    os.environ[ 'GALAXY_INSTALL_TEST_HOST' ] = galaxy_test_host
    # Set the GALAXY_TEST_HOST environment variable so that the toolbox tests will have the Galaxy url
    # on which to to run tool functional tests.
    os.environ[ 'GALAXY_TEST_HOST' ] = galaxy_test_host
    galaxy_test_port = os.environ.get( 'GALAXY_INSTALL_TEST_PORT', str( install_and_test_base_util.default_galaxy_test_port_max ) )
    os.environ[ 'GALAXY_TEST_PORT' ] = galaxy_test_port
    tool_path = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_PATH', 'tools' )
    if 'HTTP_ACCEPT_LANGUAGE' not in os.environ:
        os.environ[ 'HTTP_ACCEPT_LANGUAGE' ] = default_galaxy_locales
    galaxy_test_file_dir = os.environ.get( 'GALAXY_INSTALL_TEST_FILE_DIR', default_galaxy_test_file_dir )
    if not os.path.isabs( galaxy_test_file_dir ):
        galaxy_test_file_dir = os.path.abspath( galaxy_test_file_dir )
    use_distributed_object_store = os.environ.get( 'GALAXY_INSTALL_TEST_USE_DISTRIBUTED_OBJECT_STORE', False )
    if not os.path.isdir( galaxy_test_tmp_dir ):
        os.mkdir( galaxy_test_tmp_dir )
    # Set up the configuration files for the Galaxy instance.
    shed_tool_data_table_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_SHED_TOOL_DATA_TABLE_CONF',
                                                     os.path.join( galaxy_test_tmp_dir, 'test_shed_tool_data_table_conf.xml' ) )
    galaxy_tool_data_table_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_DATA_TABLE_CONF',
                                                       install_and_test_base_util.tool_data_table_conf )
    galaxy_tool_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_CONF',
                                            os.path.join( galaxy_test_tmp_dir, 'test_tool_conf.xml' ) )
    galaxy_job_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_JOB_CONF',
                                           os.path.join( galaxy_test_tmp_dir, 'test_job_conf.xml' ) )
    galaxy_shed_tool_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_SHED_TOOL_CONF',
                                                 os.path.join( galaxy_test_tmp_dir, 'test_shed_tool_conf.xml' ) )
    galaxy_migrated_tool_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_MIGRATED_TOOL_CONF',
                                                     os.path.join( galaxy_test_tmp_dir, 'test_migrated_tool_conf.xml' ) )
    galaxy_tool_sheds_conf_file = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_SHEDS_CONF',
                                                  os.path.join( galaxy_test_tmp_dir, 'test_tool_sheds_conf.xml' ) )
    galaxy_shed_tools_dict = os.environ.get( 'GALAXY_INSTALL_TEST_SHED_TOOL_DICT_FILE',
                                             os.path.join( galaxy_test_tmp_dir, 'shed_tool_dict' ) )
    file( galaxy_shed_tools_dict, 'w' ).write( to_json_string( {} ) )
    # Set the GALAXY_TOOL_SHED_TEST_FILE environment variable to the path of the shed_tools_dict file so that
    # test.base.twilltestcase.setUp will find and parse it properly.
    os.environ[ 'GALAXY_TOOL_SHED_TEST_FILE' ] = galaxy_shed_tools_dict
    if 'GALAXY_INSTALL_TEST_TOOL_DATA_PATH' in os.environ:
        tool_data_path = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_DATA_PATH' )
    else:
        tool_data_path = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
        os.environ[ 'GALAXY_INSTALL_TEST_TOOL_DATA_PATH' ] = tool_data_path
    # Configure the database connection and path.
    if 'GALAXY_INSTALL_TEST_DBPATH' in os.environ:
        galaxy_db_path = os.environ[ 'GALAXY_INSTALL_TEST_DBPATH' ]
    else:
        tempdir = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
        galaxy_db_path = os.path.join( tempdir, 'database' )
    # Configure the paths Galaxy needs to install and test tools.
    galaxy_file_path = os.path.join( galaxy_db_path, 'files' )
    new_repos_path = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
    galaxy_tempfiles = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
    galaxy_shed_tool_path = tempfile.mkdtemp( dir=galaxy_test_tmp_dir, prefix='shed_tools' )
    galaxy_migrated_tool_path = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
    # Set up the tool dependency path for the Galaxy instance.
    tool_dependency_dir = os.environ.get( 'GALAXY_INSTALL_TEST_TOOL_DEPENDENCY_DIR', None )
    if tool_dependency_dir is None:
        tool_dependency_dir = tempfile.mkdtemp( dir=galaxy_test_tmp_dir )
        os.environ[ 'GALAXY_INSTALL_TEST_TOOL_DEPENDENCY_DIR' ] = tool_dependency_dir
    os.environ[ 'GALAXY_TOOL_DEPENDENCY_DIR' ] = tool_dependency_dir
    if 'GALAXY_INSTALL_TEST_DBURI' in os.environ:
        database_connection = os.environ[ 'GALAXY_INSTALL_TEST_DBURI' ]
    else:
        database_connection = 'sqlite:///' + os.path.join( galaxy_db_path, 'install_and_test_repositories.sqlite' )
    kwargs = {}
    for dir in [ galaxy_test_tmp_dir ]:
        try:
            os.makedirs( dir )
        except OSError:
            pass
    print "Database connection: ", database_connection
    # Generate the shed_tool_data_table_conf.xml file.
    file( shed_tool_data_table_conf_file, 'w' ).write( install_and_test_base_util.tool_data_table_conf_xml_template )
    os.environ[ 'GALAXY_INSTALL_TEST_SHED_TOOL_DATA_TABLE_CONF' ] = shed_tool_data_table_conf_file
    # ---- Start up a Galaxy instance ------------------------------------------------------
    # Generate the tool_conf.xml file.
    file( galaxy_tool_conf_file, 'w' ).write( install_and_test_base_util.tool_conf_xml )
    # Generate the job_conf.xml file.
    file( galaxy_job_conf_file, 'w' ).write( install_and_test_base_util.job_conf_xml )
    # Generate the tool_sheds_conf.xml file, but only if a the user has not specified an existing one in the environment.
    if 'GALAXY_INSTALL_TEST_TOOL_SHEDS_CONF' not in os.environ:
        file( galaxy_tool_sheds_conf_file, 'w' ).write( install_and_test_base_util.tool_sheds_conf_xml )
    # Generate the shed_tool_conf.xml file.
    tool_conf_template_parser = string.Template( install_and_test_base_util.shed_tool_conf_xml_template )
    shed_tool_conf_xml = tool_conf_template_parser.safe_substitute( shed_tool_path=galaxy_shed_tool_path )
    file( galaxy_shed_tool_conf_file, 'w' ).write( shed_tool_conf_xml )
    os.environ[ 'GALAXY_INSTALL_TEST_SHED_TOOL_CONF' ] = galaxy_shed_tool_conf_file
    # Generate the migrated_tool_conf.xml file.
    migrated_tool_conf_xml = tool_conf_template_parser.safe_substitute( shed_tool_path=galaxy_migrated_tool_path )
    file( galaxy_migrated_tool_conf_file, 'w' ).write( migrated_tool_conf_xml )
    # Write the embedded web application's specific configuration to a temporary file. This is necessary in order for
    # the external metadata script to find the right datasets.
    kwargs = dict( admin_users = 'test@bx.psu.edu',
                   master_api_key = install_and_test_base_util.default_galaxy_master_api_key,
                   allow_user_creation = True,
                   allow_user_deletion = True,
                   allow_library_path_paste = True,
                   database_connection = database_connection,
                   datatype_converters_config_file = "datatype_converters_conf.xml.sample",
                   file_path = galaxy_file_path,
                   id_secret = install_and_test_base_util.galaxy_encode_secret,
                   job_config_file = galaxy_job_conf_file,
                   job_queue_workers = 5,
                   log_destination = "stdout",
                   migrated_tools_config = galaxy_migrated_tool_conf_file,
                   new_file_path = galaxy_tempfiles,
                   running_functional_tests = True,
                   shed_tool_data_table_config = shed_tool_data_table_conf_file,
                   shed_tool_path = galaxy_shed_tool_path,
                   template_path = "templates",
                   tool_config_file = ','.join( [ galaxy_tool_conf_file, galaxy_shed_tool_conf_file ] ),
                   tool_data_path = tool_data_path,
                   tool_data_table_config_path = galaxy_tool_data_table_conf_file,
                   tool_dependency_dir = tool_dependency_dir,
                   tool_path = tool_path,
                   tool_parse_help = False,
                   tool_sheds_config_file = galaxy_tool_sheds_conf_file,
                   update_integrated_tool_panel = False,
                   use_heartbeat = False )
    galaxy_config_file = os.environ.get( 'GALAXY_INSTALL_TEST_INI_FILE', None )
    # If the user has passed in a path for the .ini file, do not overwrite it.
    if not galaxy_config_file:
        galaxy_config_file = os.path.join( galaxy_test_tmp_dir, 'install_test_tool_shed_repositories_wsgi.ini' )
        config_items = []
        for label in kwargs:
            config_tuple = label, kwargs[ label ]
            config_items.append( config_tuple )
        # Write a temporary file, based on universe_wsgi.ini.sample, using the configuration options defined above.
        generate_config_file( 'universe_wsgi.ini.sample', galaxy_config_file, config_items )
    kwargs[ 'tool_config_file' ] = [ galaxy_tool_conf_file, galaxy_shed_tool_conf_file ]
    # Set the global_conf[ '__file__' ] option to the location of the temporary .ini file, which gets passed to set_metadata.sh.
    kwargs[ 'global_conf' ] = install_and_test_base_util.get_webapp_global_conf()
    kwargs[ 'global_conf' ][ '__file__' ] = galaxy_config_file
    # ---- Build Galaxy Application --------------------------------------------------
    if not database_connection.startswith( 'sqlite://' ):
        kwargs[ 'database_engine_option_max_overflow' ] = '20'
        kwargs[ 'database_engine_option_pool_size' ] = '10'
    kwargs[ 'config_file' ] = galaxy_config_file
    app = UniverseApplication( **kwargs )
    database_contexts.galaxy_context = app.model.context
    database_contexts.install_context = app.install_model.context
    global test_toolbox
    import functional.test_toolbox as imported_test_toolbox
    test_toolbox = imported_test_toolbox

    log.debug( "Embedded Galaxy application started..." )
    # ---- Run galaxy webserver ------------------------------------------------------
    server = None
    global_conf = install_and_test_base_util.get_webapp_global_conf()
    global_conf[ 'database_file' ] = database_connection
    webapp = buildapp.app_factory( global_conf,
                                   use_translogger=False,
                                   static_enabled=install_and_test_base_util.STATIC_ENABLED,
                                   app=app )
    # Serve the app on a specified or random port.
    if galaxy_test_port is not None:
        server = httpserver.serve( webapp, host=galaxy_test_host, port=galaxy_test_port, start_loop=False )
    else:
        random.seed()
        for i in range( 0, 9 ):
            try:
                galaxy_test_port = str( random.randint( install_and_test_base_util.default_galaxy_test_port_min,
                                                        install_and_test_base_util.default_galaxy_test_port_max ) )
                log.debug( "Attempting to serve app on randomly chosen port: %s", galaxy_test_port )
                server = httpserver.serve( webapp, host=galaxy_test_host, port=galaxy_test_port, start_loop=False )
                break
            except socket.error, e:
                if e[0] == 98:
                    continue
                raise
        else:
            raise Exception( "Unable to open a port between %s and %s to start Galaxy server" % \
                ( install_and_test_base_util.default_galaxy_test_port_min, install_and_test_base_util.default_galaxy_test_port_max ) )
    os.environ[ 'GALAXY_INSTALL_TEST_PORT' ] = galaxy_test_port
    # Start the server.
    t = threading.Thread( target=server.serve_forever )
    t.start()
    # Test if the server is up.
    for i in range( 10 ):
        # Directly test the app, not the proxy.
        conn = httplib.HTTPConnection( galaxy_test_host, galaxy_test_port )
        conn.request( "GET", "/" )
        if conn.getresponse().status == 200:
            break
        time.sleep( 0.1 )
    else:
        raise Exception( "Test HTTP server did not return '200 OK' after 10 tries" )
    log.debug( "Embedded galaxy web server started..." )
    log.debug( "The embedded Galaxy application is running on %s:%s" % ( str( galaxy_test_host ), str( galaxy_test_port ) ) )
    log.debug( "Repositories will be installed from the tool shed at %s" % str( install_and_test_base_util.galaxy_tool_shed_url ) )
    # If a tool_data_table_conf.test.xml file was found, add the entries from it into the app's tool data tables.
    if install_and_test_base_util.additional_tool_data_tables:
        app.tool_data_tables.add_new_entries_from_config_file( config_filename=install_and_test_base_util.additional_tool_data_tables,
                                                               tool_data_path=install_and_test_base_util.additional_tool_data_path,
                                                               shed_tool_data_table_config=None,
                                                               persist=False )
    now = time.strftime( "%Y-%m-%d %H:%M:%S" )
    print "####################################################################################"
    print "# %s - installation script for repositories containing tools started." % now
    if not can_update_tool_shed:
        print "# This run will not update the Tool Shed database."
    print "####################################################################################"
    install_and_test_statistics_dict, error_message = \
        install_and_test_repositories( app, galaxy_shed_tools_dict, galaxy_shed_tool_conf_file )
    if error_message:
        log.debug( error_message )
    else:
        total_repositories_processed = install_and_test_statistics_dict[ 'total_repositories_processed' ]
        all_tests_passed = install_and_test_statistics_dict[ 'all_tests_passed' ]
        at_least_one_test_failed = install_and_test_statistics_dict[ 'at_least_one_test_failed' ]
        successful_repository_installations = install_and_test_statistics_dict[ 'successful_repository_installations' ]
        successful_tool_dependency_installations = install_and_test_statistics_dict[ 'successful_tool_dependency_installations' ]
        repositories_with_installation_error = install_and_test_statistics_dict[ 'repositories_with_installation_error' ]
        tool_dependencies_with_installation_error = install_and_test_statistics_dict[ 'tool_dependencies_with_installation_error' ]
        now = time.strftime( "%Y-%m-%d %H:%M:%S" )
        print "####################################################################################"
        print "# %s - installation and test script for repositories containing tools completed." % now
        print "# Repository revisions processed: %s" % str( total_repositories_processed )
        if successful_repository_installations:
            print "# ----------------------------------------------------------------------------------"
            print "# The following %d revisions were successfully installed:" % len( successful_repository_installations )
            install_and_test_base_util.display_repositories_by_owner( successful_repository_installations )
        if all_tests_passed:
            print '# ----------------------------------------------------------------------------------'
            print "# The following %d revisions successfully passed all functional tests:" % len( all_tests_passed )
            install_and_test_base_util.display_repositories_by_owner( all_tests_passed )
        if at_least_one_test_failed:
            print '# ----------------------------------------------------------------------------------'
            print "# The following %d revisions failed at least 1 functional test:" % len( at_least_one_test_failed )
            install_and_test_base_util.display_repositories_by_owner( at_least_one_test_failed )
        if repositories_with_installation_error:
            print '# ----------------------------------------------------------------------------------'
            print "# The following %d revisions have installation errors:" % len( repositories_with_installation_error )
            install_and_test_base_util.display_repositories_by_owner( repositories_with_installation_error )
        if successful_tool_dependency_installations:
            print "# ----------------------------------------------------------------------------------"
            print "# The following %d tool dependencies were successfully installed:" % len( successful_tool_dependency_installations )
            install_and_test_base_util.display_tool_dependencies_by_name( successful_tool_dependency_installations )
        if tool_dependencies_with_installation_error:
            print "# ----------------------------------------------------------------------------------"
            print "# The following %d tool dependencies have installation errors:" % len( tool_dependencies_with_installation_error )
            install_and_test_base_util.display_tool_dependencies_by_name( tool_dependencies_with_installation_error )
        print "####################################################################################"
    log.debug( "Shutting down..." )
    # Gracefully shut down the embedded web server and UniverseApplication.
    if server:
        log.debug( "Shutting down embedded galaxy web server..." )
        server.server_close()
        server = None
        log.debug( "Embedded galaxy server stopped..." )
    if app:
        log.debug( "Shutting down galaxy application..." )
        app.shutdown()
        app = None
        log.debug( "Embedded galaxy application stopped..." )
    # Clean up test files unless otherwise specified.
    if 'GALAXY_INSTALL_TEST_NO_CLEANUP' not in os.environ:
        for dir in [ galaxy_test_tmp_dir ]:
            if os.path.exists( dir ):
                try:
                    shutil.rmtree( dir )
                    log.debug( "Cleaned up temporary files in %s", str( dir ) )
                except:
                    pass
    else:
        log.debug( 'GALAXY_INSTALL_TEST_NO_CLEANUP set, not cleaning up.' )
    # Return a "successful" response to buildbot.
    return 0

def remove_generated_tests( app ):
    """
    Delete any configured tool functional tests from the test_toolbox.__dict__, otherwise nose will find them
    and try to re-run the tests after uninstalling the repository, which will cause false failure reports,
    since the test data has been deleted from disk by now.
    """
    tests_to_delete = []
    tools_to_delete = []
    global test_toolbox
    for key in test_toolbox.__dict__:
        if key.startswith( 'TestForTool_' ):
            log.debug( 'Tool test found in test_toolbox, deleting: %s' % str( key ) )
            # We can't delete this test just yet, we're still iterating over __dict__.
            tests_to_delete.append( key )
            tool_id = key.replace( 'TestForTool_', '' )
            for tool in app.toolbox.tools_by_id:
                if tool.replace( '_', ' ' ) == tool_id.replace( '_', ' ' ):
                    tools_to_delete.append( tool )
    for key in tests_to_delete:
        # Now delete the tests found in the previous loop.
        del test_toolbox.__dict__[ key ]
    for tool in tools_to_delete:
        del app.toolbox.tools_by_id[ tool ]

def remove_install_tests():
    """
    Delete any configured repository installation tests from the test_toolbox.__dict__, otherwise nose will find them
    and try to install the repository again while running tool functional tests.
    """
    tests_to_delete = []
    global test_toolbox
    # Push all the toolbox tests to module level
    for key in test_install_repositories.__dict__:
       if key.startswith( 'TestInstallRepository_' ):
            log.debug( 'Repository installation process found, deleting: %s' % str( key ) )
            # We can't delete this test just yet, we're still iterating over __dict__.
            tests_to_delete.append( key )
    for key in tests_to_delete:
        # Now delete the tests found in the previous loop.
        del test_install_repositories.__dict__[ key ]

def test_repository_tools( app, repository, repository_dict, tool_test_results_dicts, tool_test_results_dict,
                           install_and_test_statistics_dict ):
    """Test tools contained in the received repository."""
    name = str( repository.name )
    owner = str( repository.owner )
    changeset_revision = str( repository.changeset_revision )
    repository_identifier_dict = dict( name=name, owner=owner, changeset_revision=changeset_revision )
    # Set the module-level variable 'toolbox', so that test.functional.test_toolbox will generate the appropriate test methods.
    test_toolbox.toolbox = app.toolbox
    # Generate the test methods for this installed repository. We need to pass in True here, or it will look
    # in $GALAXY_HOME/test-data for test data, which may result in missing or invalid test files.
    test_toolbox.build_tests( testing_shed_tools=True, master_api_key=install_and_test_base_util.default_galaxy_master_api_key )
    # Set up nose to run the generated functional tests.
    test_config = nose.config.Config( env=os.environ, plugins=nose.plugins.manager.DefaultPluginManager() )
    test_config.configure( sys.argv )
    # Run the configured tests.
    result, test_plugins = install_and_test_base_util.run_tests( test_config )
    if result.wasSuccessful():
        # This repository's tools passed all functional tests.  Use the ReportResults nose plugin to get a list
        # of tests that passed.
        for plugin in test_plugins:
            if hasattr( plugin, 'getTestStatus' ):
                test_identifier = '%s/%s' % ( owner, name )
                passed_tests = plugin.getTestStatus( test_identifier )
                break
        tool_test_results_dict[ 'passed_tests' ] = []
        for test_id in passed_tests:
            # Normalize the tool ID and version display.
            tool_id, tool_version = get_tool_info_from_test_id( test_id )
            test_result = dict( test_id=test_id, tool_id=tool_id, tool_version=tool_version )
            tool_test_results_dict[ 'passed_tests' ].append( test_result )
        # Update the repository_metadata table in the tool shed's database to include the passed tests.
        passed_repository_dict = repository_identifier_dict
        install_and_test_statistics_dict[ 'all_tests_passed' ].append( passed_repository_dict )
        params = dict( tools_functionally_correct=True,
                       do_not_test=False,
                       test_install_error=False )
        # Call the register_test_result() method to execute a PUT request to the repository_revisions API
        # controller with the status of the test. This also sets the do_not_test and tools_functionally
        # correct flags and updates the time_last_tested field to today's date.
        # TODO: do something useful with response_dict
        response_dict = install_and_test_base_util.register_test_result( install_and_test_base_util.galaxy_tool_shed_url,
                                                                         tool_test_results_dicts,
                                                                         tool_test_results_dict,
                                                                         repository_dict,
                                                                         params,
                                                                         can_update_tool_shed )
        log.debug( 'Revision %s of repository %s owned by %s installed and passed functional tests.' % \
            ( changeset_revision, name, owner ) )
    else:
        # The get_failed_test_dicts() method returns a list.
        failed_test_dicts = get_failed_test_dicts( result, from_tool_test=True )
        tool_test_results_dict[ 'failed_tests' ] = failed_test_dicts
        failed_repository_dict = repository_identifier_dict
        install_and_test_statistics_dict[ 'at_least_one_test_failed' ].append( failed_repository_dict )
        set_do_not_test = not is_latest_downloadable_revision( install_and_test_base_util.galaxy_tool_shed_url, repository_dict )
        params = dict( tools_functionally_correct=False,
                       test_install_error=False,
                       do_not_test=str( set_do_not_test ) )
        # TODO: do something useful with response_dict
        response_dict = install_and_test_base_util.register_test_result( install_and_test_base_util.galaxy_tool_shed_url,
                                                                         tool_test_results_dicts,
                                                                         tool_test_results_dict,
                                                                         repository_dict,
                                                                         params,
                                                                         can_update_tool_shed )
        log.debug( 'Revision %s of repository %s owned by %s installed successfully but did not pass functional tests.' % \
            ( changeset_revision, name, owner ) )
    # Run the uninstall method. This removes tool functional test methods from the test_toolbox module and uninstalls the
    # repository using Twill.
    log.debug( 'Uninstalling changeset revision %s of repository %s' % ( str( changeset_revision ), str( name ) ) )
    # We are uninstalling this repository and all of its repository dependencies.
    install_and_test_base_util.uninstall_repository_and_repository_dependencies( app, repository_dict )
    # Clean out any generated tests. This is necessary for Twill.
    remove_generated_tests( app )
    # Set the test_toolbox.toolbox module-level variable to the new app.toolbox.
    test_toolbox.toolbox = app.toolbox
    return install_and_test_statistics_dict

if __name__ == "__main__":
    # The tool_test_results_dict should always have the following structure:
    # {
    #     "test_environment":
    #         {
    #              "galaxy_revision": "9001:abcd1234",
    #              "galaxy_database_version": "114",
    #              "tool_shed_revision": "9001:abcd1234",
    #              "tool_shed_mercurial_version": "2.3.1",
    #              "tool_shed_database_version": "17",
    #              "python_version": "2.7.2",
    #              "architecture": "x86_64",
    #              "system": "Darwin 12.2.0"
    #         },
    #      "passed_tests":
    #         [
    #             {
    #                 "test_id": "The test ID, generated by twill",
    #                 "tool_id": "The tool ID that was tested",
    #                 "tool_version": "The tool version that was tested",
    #             },
    #         ]
    #     "failed_tests":
    #         [
    #             {
    #                 "test_id": "The test ID, generated by twill",
    #                 "tool_id": "The tool ID that was tested",
    #                 "tool_version": "The tool version that was tested",
    #                 "stderr": "The output of the test, or a more detailed description of what was tested and what the outcome was."
    #                 "traceback": "The captured traceback."
    #             },
    #         ]
    #     "installation_errors":
    #         {
    #              'tool_dependencies':
    #                  [
    #                      {
    #                         'type': 'Type of tool dependency, e.g. package, set_environment, etc.',
    #                         'name': 'Name of the tool dependency.',
    #                         'version': 'Version if this is a package, otherwise blank.',
    #                         'error_message': 'The error message returned when installation was attempted.',
    #                      },
    #                  ],
    #              'repository_dependencies':
    #                  [
    #                      {
    #                         'tool_shed': 'The tool shed that this repository was installed from.',
    #                         'name': 'The name of the repository that failed to install.',
    #                         'owner': 'Owner of the failed repository.',
    #                         'changeset_revision': 'Changeset revision of the failed repository.',
    #                         'error_message': 'The error message that was returned when the repository failed to install.',
    #                      },
    #                  ],
    #              'current_repository':
    #                  [
    #                      {
    #                         'tool_shed': 'The tool shed that this repository was installed from.',
    #                         'name': 'The name of the repository that failed to install.',
    #                         'owner': 'Owner of the failed repository.',
    #                         'changeset_revision': 'Changeset revision of the failed repository.',
    #                         'error_message': 'The error message that was returned when the repository failed to install.',
    #                      },
    #                  ],
    #             {
    #                 "name": "The name of the repository.",
    #                 "owner": "The owner of the repository.",
    #                 "changeset_revision": "The changeset revision of the repository.",
    #                 "error_message": "The message stored in tool_dependency.error_message."
    #             },
    #         }
    #      "missing_test_components":
    #         [
    #             {
    #                 "tool_id": "The tool ID that missing components.",
    #                 "tool_version": "The version of the tool."
    #                 "tool_guid": "The guid of the tool."
    #                 "missing_components": "Which components are missing, e.g. the test data filename, or the test-data directory."
    #             },
    #         ]
    #      "not_tested":
    #         {
    #             "reason": "The Galaxy development team has determined that this repository should not be installed and tested by the automated framework."
    #         }
    # }
    sys.exit( main() )