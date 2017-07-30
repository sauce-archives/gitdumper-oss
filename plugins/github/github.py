# encoding: utf-8
import re
import os.path
import base64
import urlparse
import requests
import traceback
import json
import hashlib
import random

config = None  # set by slack plugin config system
actions = []
responses = ("Way to go", "What were you thinking", "Y U NO put a description", "Try harder next time",
            "I can't read your mind, or your code", "All signs point to lazy", "This space intentionally left blank, thanks to",
            "No party parrot for you", "Might as well null commit" )

jira_sub = re.compile("([A-Z]{2,}-\d+)").sub  # finds JIRA keys


def process_message(data):
    try:
        _process_message(data)
    except Exception:
        traceback.print_exc()


def _process_message(data):
    if 'text' not in data:
        return

    if "https://github.com" not in data['text']:
        return

    potential_urls = [item for item in data['text'].split() if "https://github.com" in item]
    for url in potential_urls:
        url = url[1:-1]  # wrapped in "<>", cut the ends
        parsed_url = urlparse.urlparse(url)
        # example link: https://github.com/phonegap/phonegap-app-developer/blob/master/www/js/deploy.js#L7-L12
        splitzies = parsed_url.path.split('/')
        owner, repo, link_type, branch = splitzies[1:5]
        action = None
        if link_type in ["blob", "blame"]:
            filepath = '/'.join(splitzies[5:])
            lines = parsed_url.fragment.lower().replace('l', '').split('-')

            begin_line_number = None
            end_line_number = None

            if len(lines) == 1:
                if lines[0]:
                    begin_line_number = lines[0]
            elif len(lines) > 1:
                begin_line_number, end_line_number = lines

            snippet = get_snippet(
                owner, repo, filepath, begin_line_number, end_line_number, ref=branch)

            action = dict(method="files.upload",
                          channels=data['channel'],
                          filename=os.path.basename(filepath),
                          content=snippet)

        elif link_type == "pull":
            pr_number = splitzies[4]
            fragment = parsed_url.fragment
            if fragment and 'diff-' in fragment:
                match = re.search('diff-(\w+?)([LR])(\d+)$', parsed_url.fragment)
                assert match, parsed_url.fragment
                filename_hash, lr, begin_line_number = match.groups()
                end_line_number = None  # no range in PR
                master = True if lr == 'L' else False
                print(filename_hash, begin_line_number)
                snippet, filepath = get_pull_request_snippet(
                    owner, repo, pr_number, filename_hash, master,
                    begin_line_number, end_line_number)
                action = dict(method="files.upload",
                              channels=data['channel'],
                              filename=os.path.basename(filepath),
                              content=snippet)
            elif fragment and 'issuecomment' in fragment:
                frag_split = fragment.split('-')
                comment_id = frag_split[1]
                comment_data = get_comment(owner, repo, comment_id)
                attachments_dict = {
                    "author_name": comment_data['user'],
                    "author_link": "https://github.com/%s" % comment_data['user'],
                    "author_icon": comment_data['image'],
                    "text": comment_data['body'],
                }

                attachment = json.dumps([attachments_dict])
                action = dict(method="chat.postMessage",
                              as_user=True,
                              channel=data['channel'],
                              text="Pull Request #%s: Comment by %s" % (pr_number, comment_data['user']),
                              attachments=attachment)
            else:
                pr_data = get_pull_request(owner, repo, pr_number, fragment)
                color = color_code_pr(pr_data)
                if not pr_data['body']:
                    pr_data['body'] = ("This PR has no description. \n"
                                       "%s, @%s" % (random.choice(responses), pr_data['committer']))
                else:
                    # clean out any HTML comments from the PR body
                    pr_data['body'] = re.sub("<!--.*-->(\r?\n)+", "", pr_data['body'], flags=re.DOTALL)

                jira_link = r"<{}/\1|\1>".format(config['JIRA_URL'])
                title = jira_sub(jira_link, pr_data['title'])

                attachments_dict = {
                    "thumb_url": "%s" % pr_data['image'],
                    "color": "%s" % color,
                    "title": "%s" % title,
                    "text": "%s" % pr_data['body']}

                attachment = json.dumps([attachments_dict])
                action = dict(method="chat.postMessage",
                              as_user=True,
                              channel=data['channel'],
                              text="Pull Request # %s" % pr_number,
                              attachments=attachment)
        if action:
            actions.append(action)


def color_code_pr(pr_data):
    if pr_data['status']['statuses']:
        state = pr_data['status']['state']
        print state
        if state == "pending":
            color = "#ffdf66"  # yellow for tests in progress
        elif state == "success":
            color = "0066cc"  # blue for tests all passing/finished
        else:
            color = "#CC1100"  # red for failed tests
    else:
        if pr_data['mergeable']:  # green for automerge
            color = "#2F972F"
        else:
            color = "#d3d3d3"  # gray for no automerge

    return color


def get_pull_request_snippet(
        owner, repo, pull_number, filename_hash, master, begin=None, end=None):
    url_template = (
        'https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files')
    url = url_template.format(owner=owner, repo=repo, number=pull_number)
    files_data = github(url)
    for file_data in files_data:
        if hashlib.md5(file_data['filename']).hexdigest() != filename_hash:
            continue
        if master:
            ref = None
        else:
            ref = file_data['contents_url'].split('?ref=')[1]
        snippet = get_snippet(
            owner, repo, file_data['filename'], begin=begin, end=end, ref=ref)

        return snippet, file_data['filename']


def get_snippet(owner, repo, file_path, begin=None, end=None, ref=None):
    begin = int(begin) if begin else 0
    end = int(end) if end else begin

    url_template = (
        'https://api.github.com/repos/{owner}/{repo}/contents/{path}')
    url = url_template.format(owner=owner, repo=repo, path=file_path)
    if ref:
        url += '?ref=' + ref
    response_data = github(url)

    assert isinstance(response_data, dict)  # list in case of directory
    assert response_data['encoding'] == 'base64'
    content = base64.decodestring(response_data['content'])
    if begin:
        content = "\n".join(content.splitlines()[begin-1:end])

    return content


def get_pull_request(owner, repo, pull_number, line_diff):
    # TODO: lukmdo use line_diff param here, at least for now. can refactor later.
    url_template = (
        'https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}')
    url = url_template.format(owner=owner, repo=repo, pull_number=pull_number)
    pr_data = github(url)

    head_sha = pr_data['head']['sha']
    url_template = (
        'https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status')
    url = url_template.format(owner=owner, repo=repo, sha=head_sha)
    status_data = github(url)

    return {'state': pr_data['state'],
            'title': pr_data['title'],
            'body': pr_data['body'],
            'mergeable': pr_data['mergeable'],
            'status': status_data,
            'image': pr_data['user']['avatar_url'],
            'committer': pr_data['user']['login'],
            }


def get_comment(owner, repo, comment_id):
    url_template = (
        'https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}')
    url = url_template.format(owner=owner, repo=repo, comment_id=comment_id)
    comment_data = github(url)

    return {'url': comment_data['html_url'],
            'image': comment_data['user']['avatar_url'],
            'user': comment_data['user']['login'],
            'body': comment_data['body'],
            }


def github(url):
    headers = {'Authorization': 'token ' + config['GITHUB_TOKEN']}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    response_data = response.json()
    return response_data
