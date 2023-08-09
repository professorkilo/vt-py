#!/usr/local/bin/python
# Copyright © 2022 The vt-py authors. All Rights Reserved.
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

"""Manage a set of YARA nethunting rules by adding/removing domains.

This script automatically updates a set of nethunting rules (file, url, domain,
ip) in your VT account by adding and removing domains.

Read more:
https://www.virustotal.com/gui/hunting-overview
https://developers.virustotal.com/v3.0/reference#livehunt
https://developers.virustotal.com/docs/nethunt
"""

import argparse
import asyncio
import copy
import json
import os
import re
import vt
import sys


API_KEY_ENV_VAR = 'VT_API_KEY'
RULESET_PREFIX = 'auto_network_watch_'
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'netwatch_templates')
RULESET_ENTITY = ('file', 'url', 'domain', 'ip_address')
RULESET_LINK = 'https://www.virustotal.com/yara-editor/livehunt/'


def extract_domains_from_rule(rules):
  """Extract the domain list from the comment of a yara rule."""
  return json.loads(rules.split('*/')[0].split('---', 2)[1])

async def get_rulesets():
  """Retrieve a rule from VT to get currently monitored properties."""
  rulesets = {}
  async with vt.Client(os.environ.get(API_KEY_ENV_VAR)) as client:
    try:
      rulesets_it = client.iterator(
        '/intelligence/hunting_rulesets',
        params={'filter': f'name:{RULESET_PREFIX}* tag:autogenerated'},
        limit=10)

      async for ruleset in rulesets_it:
        entity = ruleset.name.split(RULESET_PREFIX)[1]
        rulesets[entity] = {'id': ruleset.id,
                          'name': ruleset.name,
                          'rules': ruleset.rules,
                          'domains': extract_domains_from_rule(ruleset.rules)}

    except vt.error.APIError as e:
      print(f'Error retrieving {RULESET_PREFIX}* rulesets: {e}')

    return rulesets


def render_template(entity, domains):
  domain_list = json.dumps(domains, indent=1)
  template = ''
  body_template = os.path.join(TEMPLATE_DIR, '_body.yara')
  with open(body_template, encoding='utf-8') as f:
    template += f.read().replace('${domain_list_json}', domain_list)
    template += '\n'

  kind_template = os.path.join(TEMPLATE_DIR, entity + '.yara')
  with open(kind_template, encoding='utf-8') as f:
    rule_block = f.read()

    for domain in domains:
      domain_escaped = re.compile(r'[^\w\d]').sub('_', domain)
      template += (rule_block.
          replace('${domain}', domain).
          replace('${domain_escaped}', domain_escaped))
      template += '\n'
  return template


async def build_rulesets(queue, rulesets, domains):
  for entity in RULESET_ENTITY:
    task = {
      'name': RULESET_PREFIX + entity,
      'entity': entity,
      'rules': render_template(entity, domains)}
    if rulesets.get(entity):
      task['id'] = rulesets[entity].get('id')
    await queue.put(task)


async def upload_rulesets(queue):
  """Uploads selected files to VirusTotal."""
  async with vt.Client(os.environ.get(API_KEY_ENV_VAR)) as client:
    while not queue.empty():
      task = await queue.get()

      name = task.get('name')
      if task.get('id'):
        ruleset = vt.Object(
            obj_type='hunting_ruleset',
            obj_attributes={
                'rules': task.get('rules')})
        try:
          # Fix for https://github.com/VirusTotal/vt-py/issues/155 issue.
          await client.patch_async(
              path='/intelligence/hunting_rulesets/' + task.get('id'),
              json_data={'data': ruleset.to_dict()})
          print(f'Ruleset {name} [{RULESET_LINK}{task["id"]}] updated.')
        except vt.error.APIError as e:
          print(f'Error updating {name}: {e}')

      else:
        ruleset = vt.Object(
            obj_type='hunting_ruleset',
            obj_attributes={
                'name': name,
                'match_object_type': task.get('entity'),
                'enabled': True,
                'tags': ('autogenerated',),
                'rules': task.get('rules')})
        try:
          result = await client.post_object_async(
              path='/intelligence/hunting_rulesets', obj=ruleset)
          print(f'Ruleset {name} [{RULESET_LINK}{result.id}] created.')
        except vt.error.APIError as e:
          print(f'Error saving {name}: {e}')

      queue.task_done()


async def main():
  parser = argparse.ArgumentParser(description=(
     'Manage a set of YARA nethunting rules by adding/removing domains.'))
  parser.add_argument('-l', '--list', action='store_true',
                      help='List current monitored domains.')
  parser.add_argument('-a', '--add-domain',
                      help='Add a domain to the list.')
  parser.add_argument('-d', '--delete-domain',
                      help='Remove a domain from the list.')
  parser.add_argument('--workers', type=int, required=False, default=4,
                      help='number of concurrent workers')
  args = parser.parse_args()

  # Verify templates exists
  for name in RULESET_ENTITY + ('_body', ):
    template_name = os.path.join(TEMPLATE_DIR, name + '.yara')
    if not os.path.exists(template_name):
      print(f'ERROR: file {template_name} not found.')
      sys.exit(1)

  if os.environ.get(API_KEY_ENV_VAR) is None:
    print(f'Please set {API_KEY_ENV_VAR} environment variable')
    return

  rulesets = await get_rulesets()
  if not rulesets and not args.add_domain:
    print('* Empty domain list, use --add-domain domain.tld to register one')
    sys.exit(1)

  domains = rulesets.get('url', {}).get('domains', [])
  if args.list:
    if not domains:
      print('* Empty domain list, use --add-domain domain.tld to register one')
      sys.exit(0)

    print('Currently monitored domains:')
    for domain in domains:
      print(f'- {domain}')
    sys.exit(0)

  new_domain_list = copy.copy(domains)
  if args.add_domain:
    new_domain_list.append(args.add_domain)

  if args.delete_domain:
    if not args.delete_domain in new_domain_list:
      print(f'* {args.delete_domain} not in list')
      sys.exit(1)
    new_domain_list.remove(args.delete_domain)

  new_domain_list = list(set(new_domain_list))
  new_domain_list.sort()

  if new_domain_list != domains:
    print('Updating monitored list:')
    for domain in new_domain_list:
      print(f'- {domain}')

    # Update the rulesets
    queue = asyncio.Queue()

    await build_rulesets(queue, rulesets, new_domain_list)

    worker_tasks = []
    for _ in range(args.workers):
      worker_tasks.append(asyncio.create_task(upload_rulesets(queue)))
    await asyncio.gather(*worker_tasks)

  else:
    print('Nothing to do')

if __name__ == '__main__':
  asyncio.run(main())