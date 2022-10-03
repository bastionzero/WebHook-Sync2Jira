# This file is part of sync2jira.
# Copyright (C) 2016 Red Hat, Inc.
#
# sync2jira is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# sync2jira is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with sync2jira; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110.15.0 USA
#
# Authors:  Ralph Bean <rbean@redhat.com>
# Built-In Modules
import logging

# 3rd Party Modules
from jira import JIRAError

# Local Modules
import sync2jira.downstream_issue as d_issue
from sync2jira.intermediary import Issue, matcher


log = logging.getLogger('sync2jira')


def format_comment(pr, pr_suffix, client, config):
    """
    Formats comment to link PR.
    :param sync2jira.intermediary.PR pr: Upstream issue we're pulling data from
    :param String pr_suffix: Suffix to indicate what state we're transitioning too
    :param jira.client.JIRA client: JIRA Client
    :return: Formatted comment
    :rtype: String
    """
    # Find the pr.reporters JIRA username
    ret = get_jira_username_from_github(config, pr.reporter)
    if ret:
        reporter = f"[~accountid:{ret}]"
    else:
        reporter = pr.reporter

    if 'closed' in pr_suffix:
        comment = f"Merge request [{pr.title.replace(']', '').replace('[', '')}|{pr.url}] was closed."
    elif 'reopened' in pr_suffix:
        comment = f"Merge request [{pr.title.replace(']', '').replace('[', '')}|{pr.url}] was reopened."
    elif 'merged' in pr_suffix:
        comment = f"Merge request [{pr.title.replace(']', '').replace('[', '')}|{pr.url}] was merged!"
    else:
        comment = f"{reporter} mentioned this issue in " \
            f"merge request [{pr.title.replace(']', '').replace('[', '')}| {pr.url}]."
    return comment


def issue_link_exists(client, existing, pr):
    """
    Checks if we've already linked this PR

    :param jira.client.JIRA client: JIRA Client
    :param jira.resources.Issue existing: Existing JIRA issue that was found
    :param sync2jira.intermediary.PR pr: Upstream issue we're pulling data from
    :returns: True/False if the issue exists/does not exists
    """
    # Query for our issue
    for issue_link in client.remote_links(existing):
        if issue_link.object.url == pr.url:
            # Issue has already been linked
            return True
    return False


def comment_exists(client, existing, new_comment):
    """
    Checks if new_comment exists in existing
    :param jira.client.JIRA client: JIRA Client
    :param jira.resources.Issue existing: Existing JIRA issue that was found
    :param String new_comment: Formatted comment we're looking for
    :returns: Nothing
    """
    # Grab and loop over comments
    comments = client.comments(existing)
    for comment in comments:
        if new_comment == comment.body:
            # If the comment was
            return True
    return False


def update_jira_issue(existing, pr, client, config):
    """
    Updates an existing JIRA issue (i.e. tags, assignee, comments etc).

    :param jira.resources.Issue existing: Existing JIRA issue that was found
    :param sync2jira.intermediary.PR pr: Upstream issue we're pulling data from
    :param jira.client.JIRA client: JIRA Client
    :param dict config: Config dict
    :returns: Nothing
    """
    # Get our updates array
    updates = pr.downstream.get('pr_updates', {})

    # Format and add comment to indicate PR has been linked
    new_comment = format_comment(pr, pr.suffix, client, config)

    # See if the issue_link and comment exists
    exists = issue_link_exists(client, existing, pr)
    comment_exist = comment_exists(client, existing, new_comment)
    # Check if the comment if already there
    if not exists:
        # Attach remote link
        remote_link = dict(url=pr.url, title=f"[PR] {pr.title}")
        d_issue.attach_link(client, existing, remote_link)
    if not comment_exist:
        log.info(f"Added comment for PR {pr.title} on JIRA {pr.jira_key}")
        client.add_comment(existing, new_comment)

    # Only synchronize link_transition for listings that op-in
    if any('merge_transition' in item for item in updates) and 'merged' in pr.suffix:
        log.info("Looking for new merged_transition")
        update_transition(client, existing, pr, 'merge_transition')

    # Only synchronize merge_transition for listings that op-in
    # and a link comment has been created
    if any('link_transition' in item for item in updates) and \
            'mentioned' in new_comment and not exists:
        log.info("Looking for new link_transition")
        update_transition(client, existing, pr, 'link_transition')


def update_transition(client, existing, pr, transition_type):
    """
    Helper function to update the transition of a downstream JIRA issue.

    :param jira.client.JIRA client: JIRA client
    :param jira.resource.Issue existing: Existing JIRA issue
    :param sync2jira.intermediary.PR pr: Upstream issue
    :param string transition_type: Transition type (link vs merged)
    :returns: Nothing
    """
    # Get our closed status
    link_status = [transition for transition in pr.downstream.get('pr_updates', []) if transition_type in transition]
    if link_status:
        closed_status = link_status[0][transition_type]

        # Update the state
        d_issue.change_status(client, existing, closed_status, pr)

        log.info(f"Updated {transition_type} for issue {pr.title}")


def sync_with_jira(pr, config):
    """
    Attempts to sync a upstream PR with JIRA (i.e. by finding
    an existing issue).

    :param sync2jira.intermediary.PR/Issue pr: PR or Issue object
    :param Dict config: Config dict
    :returns: Nothing
    """
    log.info("[PR] Considering upstream %s, %s", pr.url, pr.title)

    # Return if testing
    if config['sync2jira']['testing']:
        log.info("Testing flag is true.  Skipping actual update.")
        return None

    if not pr.match:
        log.info(f"[PR] No match found for {pr.title}")
        return None

    # Create a client connection for this issue
    client = d_issue.get_jira_client(pr, config)

    # Find our JIRA issue if one exists
    if isinstance(pr, Issue):
        pr.jira_key = matcher(pr.content, pr.comments)

    for jira_key in pr.jira_key:
        query = f"Key = {jira_key}"
        try:
            response = client.search_issues(query)
            # Continue to next potential match if 0 or more than 1 issue is found
            if len(response) == 0 or len(response) > 1:
                log.warning(f'{len(response)} JIRA issues found for {pr.title}. Query: {query}')
                continue
        except JIRAError:
            # Continue to next potential match on JIRA error
            log.warning(f'No JIRA issue exists for PR: {pr.title}. Query: {query}')
            continue
        # Existing JIRA issue is the only one in the query
        existing = response[0]  
        
        # Else start syncing relevant information
        log.info(f"Syncing PR {pr.title}")
        update_jira_issue(existing, pr, client, config)
        log.info(f"Done syncing PR {pr.title}")

def get_jira_username_from_github(config, github_login):
    """ Helper function to get JIRA username from Github login """
    for name, data in config['mapping'].items():
        if name == github_login:
            return data['jira']