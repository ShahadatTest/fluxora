"""Presentation-only workspace layouts; no ledger access or operation execution.

Existing form controls, values, order and endpoints remain in their original
markup. A lossless HTML tree adds layout wrappers around that markup. It never
interprets an amount, decides a workflow state, or hides an authority check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape, unescape
from html.parser import HTMLParser
import re


# Purpose, screen family, documentation chapter. One entry per workspace route.
SCREENS = {
    '/settings/connections': ('Review connection options for private local setup; hosted demo controls remain locked.', 'settings', 'model-configuration'),
    '/': ('Review the recorded cash position and decide what needs attention.', 'overview', 'getting-started'),
    '/today': ('Work through the current priorities, then open the supporting records.', 'review', 'getting-started'),
    '/review': ('Review cash pressure, missing facts and pending decisions in one place.', 'review', 'getting-started'),
    '/decision-brief': ('Prepare a source-linked brief for the next owner decision.', 'report', 'forecasts'),
    '/agent/tasks': ('Run a supported task in one step, or review its interpretation first. Final approval stays separate.', 'agent', 'guided-agent'),
    '/agent': ('Inspect the advanced agent, provider status and saved request history.', 'agent', 'guided-agent'),
    '/forecast': ('Inspect the cash outlook, assumptions and lowest balance for each currency.', 'report', 'forecasts'),
    '/weekly-cashflow': ('Follow cash timing across thirteen weeks without combining currencies.', 'report', 'forecasts'),
    '/account-forecast': ('Check whether the right account can fund its scheduled obligations.', 'report', 'forecasts'),
    '/variance': ('Compare saved forecasts with recorded outcomes and trace the differences.', 'report', 'forecasts'),
    '/plans': ('Compare allowed responses and inspect the conditions behind each proposal.', 'planning', 'scenarios'),
    '/scenarios': ('Combine hypothetical changes and compare them with recorded timing.', 'planning', 'scenarios'),
    '/stress': ('Test a customer delay and see where the conditional cash outlook changes.', 'planning', 'scenarios'),
    '/plans/examples': ('Explore labelled fixture plans and their feasibility constraints.', 'planning', 'scenarios'),
    '/affordability': ('Test a possible expense against currency-specific cash floors.', 'planning', 'scenarios'),
    '/calendar': ('Inspect the dated collections and obligations behind the cash forecast.', 'report', 'forecasts'),
    '/ledger': ('Inspect source records or stage new evidence for an explicit review.', 'evidence', 'evidence'),
    '/facts': ('Resolve an uncertain collection date using checked source evidence.', 'entry', 'evidence'),
    '/attachments': ('Keep original local attachments alongside their evidence references.', 'evidence', 'evidence'),
    '/freshness': ('Find stale or missing inputs before relying on a forecast.', 'review', 'evidence'),
    '/duplicates': ('Review possible duplicates against their sources before recording a decision.', 'review', 'evidence'),
    '/entries': ('Record an invoice or scheduled bill from checked evidence.', 'entry', 'evidence'),
    '/records': ('Open the current records and inspect their source-linked history.', 'evidence', 'evidence'),
    '/search': ('Find recorded items by their identifiers and source details.', 'evidence', 'evidence'),
    '/collections': ('Prioritise outstanding collections using due dates and conditional gap relief.', 'report', 'forecasts'),
    '/promises': ('Record and review source-backed customer payment promises.', 'entry', 'evidence'),
    '/holds': ('Review disputed collections and the evidence for holding or releasing them.', 'entry', 'evidence'),
    '/parties': ('Inspect a party statement with recorded obligations and cash history.', 'report', 'evidence'),
    '/party-register': ('Maintain explicit party references without guessing record matches.', 'entry', 'evidence'),
    '/split-receipts': ('Review one observed payment and its separate invoice allocations.', 'entry', 'reconciliation'),
    '/unallocated-refunds': ('Review observed refunds against an unallocated parent payment.', 'entry', 'reconciliation'),
    '/invoice-adjustments': ('Review source-backed changes to an invoice claim and its history.', 'entry', 'evidence'),
    '/refunds': ('Record source-backed refund observations separately from credit balances.', 'entry', 'reconciliation'),
    '/payments': ('Inspect bills and protected payroll before reviewing payment observations.', 'review', 'approvals'),
    '/recurring': ('Review recurring bill templates and explicitly materialise obligations.', 'entry', 'evidence'),
    '/payment-warnings': ('Review possible duplicate payments against the original sources.', 'review', 'reconciliation'),
    '/payment-corrections': ('Correct a recorded payment observation while retaining its history.', 'entry', 'reconciliation'),
    '/instalments': ('Review supplier instalments and the separate acceptance evidence they need.', 'entry', 'approvals'),
    '/bill-adjustments': ('Review source corrections to bills while respecting protected obligations.', 'entry', 'evidence'),
    '/payment-dates': ('Review a source-backed payment-date correction inside the open snapshot.', 'entry', 'reconciliation'),
    '/cash-accounts': ('Inspect account-level recorded cash and its source basis.', 'report', 'forecasts'),
    '/cash-availability': ('Separate recorded balances from cash unavailable for planning.', 'entry', 'forecasts'),
    '/daily': ('Review a new cash snapshot before confirming changes to the active ledger.', 'entry', 'evidence'),
    '/loans': ('Inspect recorded loan positions and review source-backed loan observations.', 'entry', 'evidence'),
    '/credits': ('Inspect credits and adjustments without treating them as observed refund cash.', 'report', 'reconciliation'),
    '/cash-movements': ('Review other observed cash movements with a currency, date and source.', 'entry', 'reconciliation'),
    '/statements': ('Import statement evidence and review matches to recorded movements.', 'reconciliation', 'reconciliation'),
    '/statement-mapping': ('Map statement columns and inspect the result before import.', 'reconciliation', 'reconciliation'),
    '/statement-balances': ('Compare stated opening and closing balances with recorded movements.', 'reconciliation', 'reconciliation'),
    '/batch-matching': ('Review an exact net settlement against its selected recorded movements.', 'reconciliation', 'reconciliation'),
    '/receipts': ('Keep observed receipts, matching and action reconciliation distinct.', 'reconciliation', 'reconciliation'),
    '/receipts/inbox': ('Review unallocated imported rows before confirming any cash observation.', 'reconciliation', 'reconciliation'),
    '/corrections': ('Inspect and correct receipt observations without erasing their history.', 'entry', 'reconciliation'),
    '/split-payment-corrections': ('Review a whole-payment correction separately from its allocations.', 'entry', 'reconciliation'),
    '/agent/workflows': ('Inspect pending and completed workflows, then review exact operations.', 'approval', 'approvals'),
    '/actions': ('Trace each action from DRAFT through its separate decisions and observations.', 'approval', 'approvals'),
    '/reports': ('Export a deterministic snapshot with separate currencies and source warnings.', 'report', 'forecasts'),
    '/setup': ('Prepare a new synthetic business and review the explicit replacement step.', 'settings', 'local-setup'),
    '/backups': ('Create or review local recovery copies before an explicit restore decision.', 'settings', 'recovery'),
    '/telegram': ('Link only your own private Telegram account, inspect its scope and revoke pending buttons.', 'settings', 'telegram'),
    '/telegram/demo': ('Try deterministic tools and separate owner review on disposable practice data; delivery is simulated.', 'settings', 'telegram'),
    '/docs': ('Understand the workspace, calculations and authority boundaries.', 'help', 'getting-started'),
    '/demo': ('Follow a labelled synthetic demonstration of the cash-flow workflow.', 'help', 'first-review'),
    '/usability': ('Prepare a walkthrough session and record actual participant feedback.', 'help', 'getting-started'),
    '/workspace/features': ('Find the right page for a cash decision, source update or owner review.', 'directory', 'getting-started'),
}

FAMILIES = {
    'overview': ('Workspace overview', 'Start with the recorded position'),
    'review': ('Review workspace', 'Inspect the issue and its supporting evidence'),
    'report': ('Analysis workspace', 'Read the results, assumptions and source references'),
    'planning': ('Planning workspace', 'Hypothetical changes remain separate from saved facts'),
    'evidence': ('Evidence workspace', 'Sources support facts; they do not grant authority'),
    'entry': ('Owner workbench', 'Check sources and exact inputs before confirming'),
    'agent': ('Agent workspace', 'Review interpretation before preparing work'),
    'reconciliation': ('Reconciliation workspace', 'Observation, matching and reconciliation are separate'),
    'approval': ('Decision workspace', 'Owner approval and external acceptance are separate'),
    'settings': ('Workspace settings', 'Review the effect before changing the active workspace'),
    'help': ('Workspace guide', 'Use the documented local synthetic workflow'),
    'directory': ('Workspace directory', 'Choose a task to open its dedicated page'),
}


def profile(path, active=''):
    if path == '/app': path = '/'
    if path in SCREENS: return SCREENS[path]
    if path.startswith('/agent/tasks/report/'):
        return ('Inspect this local report, its assumptions and cited evidence.', 'report', 'forecasts')
    if path.startswith('/agent/workflows/'):
        return ('Review the exact operations, private simulation and separate owner decisions.', 'approval', 'approvals')
    if path.startswith('/agent/requests/'):
        return ('Inspect the saved request and its recorded results; opening it does not retry the provider.', 'agent', 'guided-agent')
    if path.startswith('/agent/tasks/'):
        return ('Review the exact task details and the current outcome shown below.', 'review', 'guided-agent')
    if path.endswith('/preview'):
        return ('Review the exact proposed inputs and effects before choosing whether to continue.', 'review', 'evidence')
    candidates = [key for key in SCREENS if key != '/' and path.startswith(key+'/')]
    if candidates: return SCREENS[max(candidates, key=len)]
    return SCREENS.get(active, ('Review the current screen and its exact supporting details.', 'review', 'getting-started'))


_VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}


@dataclass
class Element:
    tag: str
    attrs: dict
    start: int
    open_end: int
    close_start: int = 0
    end: int = 0
    parent: Element | None = None
    children: list = field(default_factory=list)
    before: str = ''
    after: str = ''
    prefix: str = ''
    suffix: str = ''
    opening: str | None = None
    replacement: str | None = None

    def ancestors(self):
        current = self.parent
        while current:
            yield current
            current = current.parent

    def render(self, source):
        if self.replacement is not None: return self.before+self.replacement+self.after
        parts = [self.before, source[self.start:self.open_end] if self.opening is None else self.opening, self.prefix]
        cursor = self.open_end
        for child in self.children:
            parts.extend((source[cursor:child.start], child.render(source)))
            cursor = child.end
        parts.extend((source[cursor:self.close_start], self.suffix, source[self.close_start:self.end], self.after))
        return ''.join(parts)

    def add_attr(self, source, name, value):
        raw = self.opening if self.opening is not None else source[self.start:self.open_end]
        offset = raw.rfind('/>') if raw.endswith('/>') else raw.rfind('>')
        self.opening = raw[:offset]+' '+name+'="'+escape(value, quote=True)+'"'+raw[offset:]
        self.attrs[name] = value


class Markup(HTMLParser):
    """Retain exact input bytes between tag boundaries, including entities."""
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source=source;self.lines=[0]
        self.lines.extend(i+1 for i,c in enumerate(source) if c=='\n')
        self.root=Element('',{},0,0,len(source),len(source))
        self.stack=[self.root];self.elements=[]
        self.feed(source);self.close()
        for node in self.stack[1:]:node.close_start=node.end=len(source)

    def position(self):
        line,column=self.getpos()
        return self.lines[line-1]+column

    def handle_starttag(self, tag, attrs):
        start=self.position();end=start+len(self.get_starttag_text())
        node=Element(tag,dict(attrs),start,end,end,end,self.stack[-1])
        self.stack[-1].children.append(node);self.elements.append(node)
        if tag not in _VOID:self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if self.stack[-1].tag==tag and tag not in _VOID:self.stack.pop()

    def handle_endtag(self, tag):
        start=self.position();end=self.source.find('>',start)+1
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                for node in self.stack[i+1:]:node.close_start=node.end=start
                self.stack[i].close_start=start;self.stack[i].end=end
                del self.stack[i:]
                break

    def descendants(self,node,tag=None):
        return [n for n in self.elements if (tag is None or n.tag==tag) and node.start<n.start and n.end<=node.close_start]


def _text(source, node):
    return unescape(re.sub('<[^>]*>', '', source[node.open_end:node.close_start])).strip()


def _classes(node):return (node.attrs.get('class') or '').split()


def _forms(tree):
    source=tree.source
    for form in (n for n in tree.elements if n.tag=='form'):
        if 'inline' in _classes(form):continue
        controls=[n for n in tree.descendants(form) if n.tag in {'input','select','textarea','button'}]
        visible=[n for n in controls if n.tag!='input' or n.attrs.get('type','text')!='hidden']
        if not visible:continue
        check=any(n.tag=='input' and n.attrs.get('type') in {'checkbox','radio'} for n in controls)
        upload=any(n.attrs.get('type')=='file' for n in controls)
        method=(form.attrs.get('method') or 'get').lower()
        action=form.attrs.get('action') or ''
        compact=all(n.tag=='button' or (n.tag=='input' and n.attrs.get('type') in {'submit','button'}) for n in visible)
        destructive=any(n.attrs.get('name')=='decision' and n.attrs.get('value')=='reject' for n in controls) or action.rstrip('/').split('/')[-1] in {'reset','restore','retire','delete','remove'}
        label='Filter & selection' if method=='get' else 'File import' if upload else 'Review exact inputs' if check else 'Prepare your inputs'
        note='Selections change this view.' if method=='get' else 'Keep the file format and source requirements in view.' if upload else 'Read every confirmation below before choosing an action.' if check else 'Check the displayed effect before submitting.'
        kind='selection' if method=='get' else 'confirmation' if check else 'import' if upload else 'input'
        form.before+='<div class="workspace-form workspace-form--'+kind+(' workspace-form--compact' if compact else '')+(' workspace-form--destructive' if destructive else '')+'"><div class="form-heading"><b>'+label+'</b><span>'+note+'</span></div>'
        form.after='</div>'+form.after
        children=form.children
        for index,node in enumerate(children):
            if node.tag!='label':continue
            nested=[n for n in tree.descendants(node) if n.tag in {'input','select','textarea'}]
            last=node
            if not nested and index+1<len(children) and children[index+1].tag in {'input','select','textarea'}:
                candidate=children[index+1]
                if not source[node.end:candidate.start].strip():nested=[candidate];last=candidate
            if not nested:continue
            full=any(n.tag=='textarea' or n.attrs.get('type') in {'file','checkbox','radio'} for n in nested)
            source_field=any(n.attrs.get('name') in {'evidence_id','note','source_note'} for n in nested)
            consent=any(n.attrs.get('type') in {'checkbox','radio'} for n in nested)
            classes='form-field'+(' form-field--wide' if full else '')+(' form-field--source' if source_field else '')+(' form-field--check' if consent else '')
            node.before+='<div class="'+classes+'">'
            last.after='</div>'+last.after


def _tables(tree):
    source=tree.source
    used={n.attrs['id'] for n in tree.elements if n.attrs.get('id')}
    for index,table in enumerate((n for n in tree.elements if n.tag=='table'),1):
        ancestors=list(table.ancestors())
        if any(n.tag=='table' for n in ancestors):continue
        heading=next((n for n in reversed(tree.elements) if n.tag in {'h2','h3'} and n.end<=table.start),None)
        title=_text(source,heading) if heading else 'Recorded details'
        title=title[:160]
        rows=[n for n in tree.descendants(table,'tr') if any(c.tag=='td' for c in n.children)]
        editable=any(n.tag in {'input','select','textarea','button','form'} for n in tree.descendants(table)) or any(n.tag=='form' for n in ancestors)
        ident='workspace-table-'+str(index)
        while ident in used:ident+='-view'
        used.add(ident)
        table.before+='<section class="data-table" aria-label="'+escape(title,quote=True)+'"><div class="table-toolbar"><div><b>'+escape(title)+'</b><span>'+str(len(rows))+' rows shown</span></div>'
        if not editable and len(rows)>5:
            table.before+='<label class="table-filter" hidden>Filter rows<input type="search" data-table-filter="'+ident+'" placeholder="Find in this table…" autocomplete="off"></label>'
        table.before+='</div><p class="table-filter-status" data-table-status="'+ident+'" role="status" hidden></p><div class="table-scroll" role="region" tabindex="0" aria-label="'+escape(title,quote=True)+'; scroll horizontally"><div id="'+ident+'">'
        table.after='</div></div><div class="table-footnote">'+('Editable rows · review the exact controls above.' if editable else 'Original row order · currencies and totals remain as recorded.')+'</div></section>'+table.after
        for cell in tree.descendants(table,'th'):
            if 'scope' not in cell.attrs:cell.add_attr(source,'scope','col')


def _related(tree):
    # Only navigation-only paragraphs are grouped. Financial prose, warnings,
    # forms, source citations and confirmation text never enter a disclosure.
    source=tree.source
    for node in tree.elements:
        if node.tag!='p' or node.attrs or any(n.tag in {'form','details','table'} for n in node.ancestors()):continue
        links=node.children
        if len(links)<5 or any(n.tag!='a' or n.attrs.get('href') not in SCREENS or n.attrs.get('class') for n in links):continue
        remaining=source[node.open_end:node.close_start]
        for link in reversed(links):remaining=remaining[:link.start-node.open_end]+remaining[link.end-node.open_end:]
        if unescape(remaining).strip(' \t\r\n·|•—–-'):continue
        node.before+='<details class="related-pages"><summary>Related workspace pages <span>'+str(len(links))+'</span></summary><nav aria-label="Related workspace pages">'
        node.after='</nav></details>'+node.after


def _card_layouts(tree, family):
    if family not in {'entry','review','evidence','reconciliation','settings'}:return
    for parent in [tree.root]+tree.elements:
        run=[]
        def flush():
            if len(run)>1:
                run[0].before='<div class="record-grid">'+run[0].before
                run[-1].after+='</div>'
            run.clear()
        for node in parent.children:
            inside=tree.descendants(node)
            eligible='card' in _classes(node) and any(n.tag=='form' for n in inside) and not any(n.tag in {'table','pre','svg'} for n in inside)
            if not eligible or (run and tree.source[run[-1].end:node.start].strip()):flush()
            if eligible:run.append(node)
        flush()


def _notices(tree):
    for node in tree.elements:
        if node.tag!='p' or 'role' in node.attrs or any(n.tag in {'table','form'} for n in node.ancestors()):continue
        classes=_classes(node)
        if 'err' in classes:node.add_attr(tree.source,'role','alert')
        elif 'ok' in classes:node.add_attr(tree.source,'role','status')


def render(title, body, path, active='', group='Workspace'):
    """Return decorated HTML, preserving every existing submission control."""
    tree=Markup(body)
    # A malformed input should remain inspectable rather than being rewritten.
    if tree.root.render(body)!=body:return body
    _notices(tree)
    if path=='/agent/tasks' and any('copilot-home' in _classes(n) for n in tree.elements):
        _forms(tree);_tables(tree);_related(tree)
        return '<div class="feature-body feature-body--agent">'+tree.root.render(body)+'</div>'
    h1=next((n for n in tree.elements if n.tag=='h1'),None)
    purpose,family,doc=profile(path,active)
    if h1 is None:
        original_heading='<h1>'+escape(title)+'</h1>'
        purpose,family,doc='Read the message below before returning to the relevant workspace.','review','troubleshooting'
    else:original_heading=body[h1.start:h1.end]
    if h1 is not None and family!='overview':
        h1.replacement=''
        prior=next((n for n in reversed(tree.elements) if n.end<=h1.start and n.parent is h1.parent),None)
        if prior and 'eyebrow' in _classes(prior) and not body[prior.end:h1.start].strip():prior.replacement=''
    family_title,guide=FAMILIES[family]
    anchors=[];used={n.attrs['id'] for n in tree.elements if n.attrs.get('id')}
    for node in tree.elements:
        if node.tag!='h2' or any(n.tag=='details' for n in node.ancestors()):continue
        text=_text(body,node)
        if not text:continue
        ident=node.attrs.get('id')
        if not ident:
            index=len(anchors)+1;ident='workspace-section-'+str(index)
            while ident in used:index+=1;ident='workspace-section-'+str(index)
            node.add_attr(body,'id',ident);used.add(ident)
        anchors.append((ident,text[:100]))
    _forms(tree);_tables(tree);_related(tree);_card_layouts(tree,family)
    if family=='overview':return tree.root.render(body)
    head='<header class="feature-header"><div><div class="eyebrow">'+escape(group)+' <span>/</span> '+escape(family_title)+'</div>'+original_heading+'<p>'+escape(purpose)+'</p></div><a class="btn feature-help" href="/docs/'+doc+'">Page guide <span aria-hidden="true">↗</span></a></header>'
    tools='<div class="feature-tools"><span class="workspace-guide"><span class="guide-marker" aria-hidden="true"></span> '+escape(guide)+'</span>'
    if len(anchors)>1:
        tools+='<details class="page-outline"><summary>On this page <span>'+str(len(anchors))+'</span></summary><nav aria-label="On this page">'+''.join('<a href="#'+escape(ident,quote=True)+'">'+escape(text)+'</a>' for ident,text in anchors)+'</nav></details>'
    tools+='</div>'
    return head+tools+'<div class="feature-body feature-body--'+family+'">'+tree.root.render(body)+'</div>'
