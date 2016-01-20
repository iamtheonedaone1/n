#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Semi-automatically converts Chrome Apps into progressive web apps.

Guides a developer through converting their existing Chrome App into a
progressive web app.
"""

from __future__ import print_function, division, unicode_literals

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys

import bs4
import colorama

import chrome_app.apis
import chrome_app.manifest
import configuration
import polyfill_manifest

# Chrome APIs with polyfills available.
POLYFILLS = {
  'notifications',
  'power',
  'runtime',
  'storage',
  'tts',
}

# Manifest filenames.
CA_MANIFEST_FILENAME = chrome_app.manifest.MANIFEST_FILENAME
PWA_MANIFEST_FILENAME = 'manifest.webmanifest'

# Name of the service worker registration script.
REGISTER_SCRIPT_NAME = 'register_sw.js'

# Name of the main service worker script.
SW_SCRIPT_NAME = 'sw.js'

# Name of the service worker static script.
SW_STATIC_SCRIPT_NAME = 'sw_static.js'

# Largest number that the cache version can be.
MAX_CACHE_VERSION = 1000000

# Where this file is located (so we can find resources).
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

SW_FORMAT_STRING = """/**
 * @file Service worker generated by Caterpillar.
 */

/**
 * @const Current cache version.
 *
 * Increment this to force cache to clear.
 */
var CACHE_VERSION = {cache_version};

/**
 * @const Object mapping a cache identifier to the actual, versioned cache name.
 */
var CACHES = {{
  'app': 'app-cache-v' + CACHE_VERSION
}};

/**
 * @const An array of filenames of cached files.
 */
var CACHED_FILES = [
  {joined_filepaths}
];

importScripts('{boilerplate_dir}/caterpillar.js');
importScripts('{boilerplate_dir}/sw_static.js');

// Ignore calls to chrome.app.runtime.onLaunched.addListener from the background
// scripts.
chrome.app = {{
  runtime: {{
    onLaunched: {{
      addListener: function() {{}}
    }}
  }}
}};

// TODO: (Caterpillar) Edit background scripts to remove chrome.app.runtime
// dependence.
"""

def setup_output_dir(input_dir, output_dir, boilerplate_dir, force=False):
  """Sets up the output web app directory tree.

  Copies all files from the input Chrome App to the output web app, and creates
  a subdirectory for the boilerplate code.

  Args:
    input_dir: String path to input Chrome App directory.
    output_dir: String path to output web app directory.
    boilerplate_dir: String path where Caterpillar's scripts should be put
      relative to output_dir.
    force: Whether to force overwrite existing output files. Default is False.

  Raises:
    OSError: Input Chrome App directory does not exist.
    OSError: Output web app directory already exists.
  """
  # Remove the output directory if it already exists.
  if force:
    logging.debug('Removing output directory tree `%s`.', output_dir)
    shutil.rmtree(output_dir, ignore_errors=True)
  elif os.path.exists(output_dir):
    # OSError is consistent with the behaviour of shutil, which raises an
    # OSError in this circumstance.
    raise OSError('Output directory already exists.')

  # Copy all files across from the Chrome App.
  logging.debug('Copying input tree `%s` to output tree `%s`.', input_dir,
                output_dir)
  shutil.copytree(input_dir, output_dir)

  # Set up the boilerplate directory.
  conv_dir = os.path.join(output_dir, boilerplate_dir)
  logging.debug('Making Caterpillar directory `%s`.', conv_dir)
  os.mkdir(conv_dir)
  polyfill_dir = os.path.join(conv_dir, 'polyfills')
  os.mkdir(polyfill_dir)

  logging.debug('Finished setting up output directory `%s`.', output_dir)

def cleanup_output_dir(output_dir):
  """Clean up the output web app by removing unnecessary files.

  Args:
    output_dir: Path to output web app directory.
  """
  logging.debug('Deleting Chrome App manifest `%s`.', CA_MANIFEST_FILENAME)
  os.remove(os.path.join(output_dir, CA_MANIFEST_FILENAME))

def copy_static_code(static_code_paths, output_dir, boilerplate_dir):
  """Copies static scripts from Caterpillar into a web app.

  Args:
    static_code_paths: List of paths to scripts to copy, relative to
      Caterpillar's JS source folder.
    output_dir: Directory of web app to copy scripts into.
    boilerplate_dir: Caterpillar script directory within the web app.
  """
  for static_code_path in static_code_paths:
    source_path = os.path.join(SCRIPT_DIR, 'js', static_code_path)
    destination_path = os.path.join(output_dir, boilerplate_dir,
                                    static_code_path)
    logging.debug('Copying `%s` to `%s`.', source_path, destination_path)
    shutil.copyfile(source_path, destination_path)

def generate_web_manifest(manifest, start_url):
  """Generates a progressive web app manifest based on a Chrome App manifest.

  Args:
    manifest: Chrome App manifest dictionary.
    start_url: URL of start page.

  Returns:
    Web manifest JSON dictionary.
  """
  pwa_manifest = {}
  pwa_manifest['name'] = manifest['name']
  pwa_manifest['short_name'] = manifest.get('short_name', manifest['name'])
  pwa_manifest['lang'] = manifest.get('default_locale', 'en')
  pwa_manifest['splash_screens'] = []
  # TODO(alger): Guess display mode from chrome.app.window.create calls
  pwa_manifest['display'] = 'minimal-ui'
  pwa_manifest['orientation'] = 'any'
  # TODO(alger): Guess start_url from chrome.app.window.create calls
  pwa_manifest['start_url'] = start_url
  # TODO(alger): Guess background/theme colour from the main page's CSS.
  pwa_manifest['theme_color'] = 'white'
  pwa_manifest['background_color'] = 'white'
  pwa_manifest['related_applications'] = []
  pwa_manifest['prefer_related_applications'] = False
  pwa_manifest['icons'] = []
  if 'icons' in manifest:
    for icon_size in manifest['icons']:
      pwa_manifest['icons'].append({
        'src': manifest['icons'][icon_size],
        'sizes': '{0}x{0}'.format(icon_size)
      })

  # TODO(alger): I've only looked at some of the manifest members here; probably
  # a bad idea to ignore the ones that don't copy across. Should give a warning.

  return pwa_manifest

def polyfill_filename(api):
  """Gets the filename associated with an API polyfill.

  Args:
    api: String name of API.

  Returns:
    Filename of API polyfill.
  """
  return "{}.polyfill.js".format(api)

def inject_script_tags(soup, required_js_paths, root_path, boilerplate_dir,
    html_path):
  """
  Injects script tags into an HTML document.

  Args:
    soup: BeautifulSoup HTML document. Will be modified.
    required_js_paths: Paths to required script files, relative to Caterpillar's
      boilerplate script directory. These will be injected in order.
    root_path: Path to the root directory of the web app from this HTML file.
      This can be either absolute or relative.
    boilerplate_dir: Caterpillar script directory within the web app.
    html_path: Path to the HTML document being modified.
  """
  if not required_js_paths:
    return  # Guarantees we have at least one script tag to inject.

  # These scripts should come before the first script tag in the document.
  # That script tag *should* be in the body, but it could be anywhere, so we
  # have to search the whole file.
  scripts = soup('script')
  first_script = scripts[0] if scripts else None

  logging.debug('Requiring scripts: %s', ', '.join(required_js_paths))

  # Insert the script tags in order.
  for script_path in reversed(required_js_paths):
    logging.debug('Inserting `%s` script tag.', script_path)
    path = os.path.join(root_path, boilerplate_dir, script_path)
    script = soup.new_tag('script', src=path)
    if first_script is None:
      soup.body.append(script)
    else:
      first_script.insert_before(script)
    first_script = script
    logging.debug('Injected `%s` script into `%s`.', script_path, html_path)

def inject_misc_tags(soup, ca_manifest, root_path, html_path):
  """
  Injects meta and link tags into an HTML document.

  Args:
    soup: BeautifulSoup HTML document. Will be modified.
    ca_manifest: Manifest dictionary of _Chrome App_.
    root_path: Path to the root directory of the web app from this HTML file.
      This can be either absolute or relative.
    html_path: Path to the HTML document being modified.
  """
  # Add manifest link tag.
  manifest_path = os.path.join(root_path, PWA_MANIFEST_FILENAME)
  manifest_link = soup.new_tag('link', rel='manifest', href=manifest_path)
  soup.head.append(manifest_link)

  # Add meta tags (if they don't already exist).
  for tag in ('description', 'author', 'name'):
    if tag in ca_manifest and not soup('meta', {'name': tag}):
      meta = soup.new_tag('meta', content=ca_manifest[tag])
      meta['name'] = tag
      soup.head.append(meta)
      logging.debug('Injected `%s` tag into `%s` with content `%s`.', tag,
        html_path, ca_manifest[tag])
  if not soup('meta', {'charset': True}):
    meta_charset = soup.new_tag('meta', charset='utf-8')
    soup.head.insert(0, meta_charset)

def insert_todos_into_file(js_path):
  """Inserts TODO comments in a JavaScript file.

  The TODO comments inserted should draw attention to places in the converted
  app that the developer will need to edit to finish converting their app.

  Args:
    js_path: Path to JavaScript file.
  """
  with open(js_path, 'rU') as in_js:
    # This search is very naïve and will only check line-by-line if there
    # are easily spotted Chrome Apps API function calls.
    out_js = []
    for line_no, line in enumerate(in_js):
      api_call = chrome_app.apis.api_member_used(line)
      if api_call is not None:
        # Construct a TODO comment.
        todo = '// TODO(Caterpillar): Check usage of {}.\n'.format(api_call)
        logging.debug('Inserting TODO in `%s:%d`:\n\t%s', js_path, line_no,
                      todo)
        out_js.append(todo)
      out_js.append(line)

  with open(js_path, 'w') as js_file:
    logging.debug('Writing modified file `%s`.', js_path)
    js_file.write(''.join(out_js))

def insert_todos_into_directory(output_dir):
  """Inserts TODO comments in all JavaScript code in a web app.

  The TODO comments inserted should draw attention to places in the converted
  app that the developer will need to edit to finish converting their app.

  Args:
    output_dir: Directory of the web app to insert TODOs into.
  """
  logging.debug('Inserting TODOs.')
  dirwalk = os.walk(output_dir)
  for (dirpath, _, filenames) in dirwalk:
    for filename in filenames:
      if filename.endswith('.js'):
        path = os.path.join(dirpath, filename)
        insert_todos_into_file(path)

def generate_service_worker(output_dir, ca_manifest, polyfill_paths,
                            boilerplate_dir):
  """Generates code for a service worker.

  Args:
    output_dir: Directory of the web app that this service worker will run in.
    ca_manifest: Chrome App manifest dictionary.
    polyfill_paths: List of paths to required polyfill scripts, relative to the
      boilerplate directory.
    boilerplate_dir: Caterpillar script directory within output web app.

  Returns:
    JavaScript string.
  """
  # Get the paths of files we will cache.
  all_filepaths = []
  logging.debug('Looking for files to cache.')
  dirwalk = os.walk(output_dir)
  for (dirpath, _, filenames) in dirwalk:
    # Add the relative file paths of each file to the filepaths list.
    all_filepaths.extend(
      os.path.relpath(os.path.join(dirpath, filename), output_dir)
      for filename in filenames)
  logging.debug('Cached files:\n\t%s', '\n\t'.join(all_filepaths))
  # Format the file paths as JavaScript strings.
  all_filepaths = ["'{}'".format(fp) for fp in all_filepaths]

  logging.debug('Generating service worker.')

  sw_js = SW_FORMAT_STRING.format(
    cache_version=random.randrange(MAX_CACHE_VERSION),
    joined_filepaths=',\n  '.join(all_filepaths),
    boilerplate_dir=boilerplate_dir
  )

  # The polyfills we get as input are relative to the boilerplate directory, but
  # the service worker is in the root directory, so we need to change the paths.
  polyfills_paths = [os.path.join(boilerplate_dir, path)
                     for path in polyfill_paths]

  background_scripts = ca_manifest['app']['background'].get('scripts', [])
  for script in polyfill_paths + background_scripts:
    logging.debug('Importing `%s` to the service worker.', script)
    sw_js += "importScripts('{}');\n".format(script)

  return sw_js

def copy_script(script, directory):
  """Copies a script from Caterpillar into the given directory.

  Args:
    script: Caterpillar JavaScript filename.
    directory: Path to directory.
  """
  path = os.path.join(SCRIPT_DIR, 'js', script)
  new_path = os.path.join(directory, script)
  logging.debug('Writing `%s` to `%s`.', path, new_path)
  shutil.copyfile(path, new_path)

def add_service_worker(output_dir, ca_manifest, polyfill_paths,
                       boilerplate_dir):
  """Adds service worker scripts to a web app.

  Args:
    output_dir: Path to web app to add service worker scripts to.
    ca_manifest: Chrome App manifest dictionary.
    polyfill_paths: List of paths to required polyfill scripts, relative to the
      boilerplate directory.
    boilerplate_dir: Caterpillar script directory within web app.
  """
  # We have to copy the other scripts before we generate the service worker
  # caching script, or else they won't be cached.
  boilerplate_path = os.path.join(output_dir, boilerplate_dir)
  copy_script(REGISTER_SCRIPT_NAME, boilerplate_path)
  copy_script(SW_STATIC_SCRIPT_NAME, boilerplate_path)

  sw_js = generate_service_worker(output_dir, ca_manifest, polyfill_paths,
                                  boilerplate_dir)

  # We can now write the service worker. Note that it must be in the root.
  sw_path = os.path.join(output_dir, SW_SCRIPT_NAME)
  logging.debug('Writing service worker to `%s`.', sw_path)
  with open(sw_path, 'w') as sw_file:
    sw_file.write(sw_js)

class InstallationError(Exception):
  """Exception raised when a dependency fails to install."""
  pass

def install_dependency(call, output_dir):
  """Installs a dependency into a directory.

  Assumes that there is no output on stdout if installation fails.

  Args:
    call: List of arguments to call to install the dependency, e.g.
      ['npm', 'install', 'bower'].
    output_dir: Directory to install into.

  Raises:
    InstallationError
  """
  popen = subprocess.Popen(call, cwd=output_dir, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
  stdout, stderr = popen.communicate()

  # Pass info and errors through to the debug log.
  for line in stdout.split(b'\n'):
    if line:
      logging.debug('%s: %s', call[0], line)
  for line in stderr.split(b'\n'):
    if line:
      logging.debug('%s err: %s', call[0], line)

  # If installation failed, stdout will be empty.
  if not stdout:
    raise InstallationError(
      'Failed to install with command: `{}`.'.format(' '.join(call)))

def install_dependencies(dependencies, output_dir):
  """Installs dependencies into a directory.

  Args:
    dependencies: List of dependency dictionaries, which are of the form
      {'name': dependency name, 'path': path to dependency once installed,
       'manager': 'bower' or 'npm'}.
    output_dir: Directory to install dependencies into.

  Raises:
    ValueError if a dependency manager is not bower or npm.
  """
  logging.debug('Installing dependencies.')
  for dependency in dependencies:
    logging.debug('Installing `%s`.', dependency['name'])
    try:
      if dependency['manager'] == 'bower':
        install_dependency(['bower', 'install', dependency['name']], output_dir)
      elif dependency['manager'] == 'npm':
        install_dependency(['npm', 'install', dependency['name']], output_dir)
      else:
        raise ValueError('Invalid dependency: No such manager `{}`.'.format(
            dependency['manager']))
    except InstallationError:
      logging.warning('Failed to install dependency `%s` with %s',
                      dependency['name'],
                      dependency['manager'])

def polyfill_paths(apis):
  """Returns a list of paths of polyfills of the given APIs.

  Args:
    apis: List of Chrome Apps API names. Examples: chrome.tts is 'tts';
      chrome.app.runtime is 'app.runtime'.

  Returns:
    List of paths to polyfills, relative to Caterpillar.

  Raises:
    ValueError if an API cannot be polyfilled.
  """
  return [os.path.join('polyfills', polyfill_filename(api))
          for api in apis]

def edit_code(output_dir, required_js_paths, ca_manifest, config):
  """Directly edits the code of the output web app.

  All editing of user code should be called from this function.

  Args:
    output_dir: Path to web app.
    required_js_paths: Paths of scripts to be included in the web app, relative
      to Caterpillar's boilerplate directory in the output web app.
    ca_manifest: Manifest dictionary of the _Chrome App_.
    config: Configuration dictionary.
  """
  logging.debug('Editing web app code.')

  # Walk the app for JS and HTML.
  # Insert TODOs into JS.
  # Inject script and meta tags into HTML.
  dirwalk = os.walk(output_dir)
  for (dirpath, _, filenames) in dirwalk:
    for filename in filenames:
      path = os.path.join(dirpath, filename)
      root_path = os.path.relpath(output_dir, dirpath)
      if filename.endswith('.js'):
        insert_todos_into_file(path)
      elif filename.endswith('.html'):
        logging.debug('Editing `%s`.', path)
        with open(path) as html_file:
          soup = bs4.BeautifulSoup(html_file.read())
        inject_script_tags(soup, required_js_paths, root_path,
                           config['boilerplate_dir'], path)
        inject_misc_tags(soup, ca_manifest, root_path, path)
        logging.debug('Writing edited and prettified `%s`.', path)
        with open(path, 'w') as html_file:
          html_file.write(soup.prettify())

# Main functions.

def convert_app(input_dir, output_dir, config, force=False):
  """Converts a Chrome App into a progressive web app.

  Args:
    input_dir: Path to input Chrome App directory.
    output_dir: Path to output web app directory.
    config: Configuration dictionary.
    force: Whether to force overwrite existing output files. Default is False.
  """
  try:
    setup_output_dir(input_dir, output_dir, config['boilerplate_dir'], force)
  except OSError as e:
    logging.error(e.message)
    return

  boilerplate_dir = config['boilerplate_dir']

  # Determine which Chrome Apps APIs are being used in the Chrome App.
  apis = chrome_app.apis.app_apis(output_dir)
  if apis:
    logging.info('Found Chrome APIs: %s', ', '.join(apis))

  # Determine which Chrome Apps APIs can be polyfilled, and which cannot.
  polyfillable = []
  not_polyfillable = []
  for api in apis:
    if api in POLYFILLS:
      polyfillable.append(api)
    else:
      not_polyfillable.append(api)

  logging.info('Polyfilled Chrome APIs: %s', ', '.join(polyfillable))
  logging.warning('Could not polyfill Chrome APIs: %s',
                  ', '.join(not_polyfillable))

  # List of paths of static code to be copied from Caterpillar into the output
  # web app, relative to Caterpillar's JS source directory.
  required_js_paths = [
    'caterpillar.js',
    REGISTER_SCRIPT_NAME,
  ] + polyfill_paths(polyfillable)

  # Read in and check the manifest file.
  try:
    ca_manifest = chrome_app.manifest.get(input_dir)
    chrome_app.manifest.verify(ca_manifest)
  except ValueError as e:
    logging.error(e.message)
    return

  # TODO(alger): Identify background scripts and determine start_url.
  start_url = config['start_url']
  logging.info('Got start URL from config file: `%s`', start_url)

  # Generate a progressive web app manifest.
  pwa_manifest = generate_web_manifest(ca_manifest, start_url)
  pwa_manifest_path = os.path.join(output_dir, PWA_MANIFEST_FILENAME)
  with open(pwa_manifest_path, 'w') as pwa_manifest_file:
    json.dump(pwa_manifest, pwa_manifest_file, indent=4, sort_keys=True)
  logging.debug('Wrote `%s` to `%s`.', PWA_MANIFEST_FILENAME, pwa_manifest_path)

  # Remove unnecessary files from the output web app. This must be done before
  # the service worker is generated, or these files will be cached.
  cleanup_output_dir(output_dir)

  # Edit the HTML and JS code of the output web app.
  # This is adding TODOs, injecting tags, etc. - anything that involves editing
  # user code directly. This must be done before the static code is copied
  # across, or the polyfills will have TODOs added to them.
  edit_code(output_dir, required_js_paths, ca_manifest, config)

  # We want the static SW file to be copied in too, so we add it here.
  # We have to add it after edit_code or it would be included in the HTML, but
  # this is service worker-only code, and shouldn't be included there.
  required_js_paths.append(SW_STATIC_SCRIPT_NAME)

  # Copy static code from Caterpillar into the output web app.
  # This must be done before the service worker is generated, or these files
  # will not be cached.
  copy_static_code(required_js_paths, output_dir, boilerplate_dir)

  # Read in the polyfill manifests and install polyfill dependencies.
  # This must be done after editing code (or the dependencies will also be
  # edited) and before the service worker is generated (or the dependencies
  # won't be cached).
  polyfill_manifests = polyfill_manifest.load_many(polyfillable)
  dependencies = [dependency
                  for manifest in polyfill_manifests.values()
                  for dependency in manifest['dependencies']]
  try:
    install_dependencies(dependencies, output_dir)
  except ValueError as e:
    logging.error(e.message)
    return

  # Generate and write a service worker.
  add_service_worker(output_dir, ca_manifest, polyfill_paths(polyfillable),
                     boilerplate_dir)

  logging.info('Conversion complete.')

class Formatter(logging.Formatter):
  """Caterpillar logging formatter.

  Adds color to the logged information.
  """
  def format(self, record):
    style = ''
    if record.levelno == logging.ERROR:
      style = colorama.Fore.RED + colorama.Style.BRIGHT
    elif record.levelno == logging.WARNING:
      style = colorama.Fore.YELLOW + colorama.Style.BRIGHT
    elif record.levelno == logging.INFO:
      style = colorama.Fore.BLUE
    elif record.levelno == logging.DEBUG:
      style = colorama.Fore.CYAN + colorama.Style.DIM

    return style + super(Formatter, self).format(record)

def main():
  """Executes the script and handles command line arguments."""
  # Set up parsers, then parse the command line arguments.
  desc = 'Semi-automatically convert Chrome Apps into progressive web apps.'
  parser = argparse.ArgumentParser(description=desc)
  parser.add_argument('-v', '--verbose', help='Verbose logging',
                      action='store_true')
  subparsers = parser.add_subparsers(dest='mode')

  parser_convert = subparsers.add_parser(
    'convert', help='Convert a Chrome App into a progressive web app.')
  parser_convert.add_argument('input', help='Chrome App input directory')
  parser_convert.add_argument(
    'output', help='Progressive web app output directory')
  parser_convert.add_argument('-c', '--config', help='Configuration file',
                              required=True, metavar='config')
  parser_convert.add_argument('-f', '--force', help='Force output overwrite',
                              action='store_true')

  parser_config = subparsers.add_parser(
    'config', help='Print a default configuration file to stdout.')
  parser_config.add_argument('output', help='Output config file path')
  parser_config.add_argument('-i', '--interactive',
    help='Whether to interactively generate the config file',
    action='store_true')

  args = parser.parse_args()

  # Set up logging.
  logging_level = logging.DEBUG if args.verbose else logging.INFO
  logging.root.setLevel(logging_level)
  colorama.init(autoreset=True)
  logging_format = ':%(levelname)s:  \t%(message)s'
  formatter = Formatter(logging_format)
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(formatter)
  logging.root.addHandler(handler)

  # Main program.
  if args.mode == 'config':
    configuration.generate_and_save(args.output, args.interactive)

  elif args.mode == 'convert':
    config = configuration.load(args.config)
    convert_app(args.input, args.output, config, args.force)

if __name__ == '__main__':
  sys.exit(main())
