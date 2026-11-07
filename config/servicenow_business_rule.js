// ServiceNow business rule that sends new incidents to the triage agent.
// Table: incident. When: async, on insert. Filter: priority is 1 or 2. Advanced: true.
// Create admin-only system properties triage.webhook_url (base address) and triage.webhook_token (shared secret).
(function executeRule(current, previous) {
    var url = gs.getProperty('triage.webhook_url');
    var token = gs.getProperty('triage.webhook_token');
    if (!url || !token) {
        gs.warn('Triage webhook not configured; skipping ' + current.getValue('number'));
        return;
    }
    var body = {
        number: current.getValue('number'),
        short_description: current.getValue('short_description'),
        description: current.getValue('description'),
        cmdb_ci: current.cmdb_ci.getDisplayValue(),
        priority: current.priority.getDisplayValue(),
        opened_at: current.getValue('opened_at'),
        url: gs.getProperty('glide.servlet.uri') + 'incident.do?sys_id=' + current.getUniqueValue()
    };
    var request = new sn_ws.RESTMessageV2();
    request.setEndpoint(url + '/webhooks/servicenow');
    request.setHttpMethod('post');
    request.setRequestHeader('Content-Type', 'application/json');
    request.setRequestHeader('X-Triage-Token', token);
    request.setRequestBody(JSON.stringify(body));
    request.setHttpTimeout(10000);
    var status = request.execute().getStatusCode();
    if (status != 202) {
        gs.warn('Triage webhook returned ' + status + ' for ' + current.getValue('number'));
    }
})(current, previous);
