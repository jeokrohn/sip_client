/* Rewrite the Local Gateway OTG URI parameter out of REGISTER To headers. */

#include <pjsua-lib/pjsua.h>
#include <pjsip/sip_module.h>
#include <pjsip/sip_uri.h>

static pj_bool_t uri_has_otg(pjsip_uri *uri)
{
    pjsip_sip_uri *sip_uri;
    pj_str_t name = pj_str("otg");

    if (!PJSIP_URI_SCHEME_IS_SIP(uri) && !PJSIP_URI_SCHEME_IS_SIPS(uri))
        return PJ_FALSE;

    sip_uri = (pjsip_sip_uri *)pjsip_uri_get_uri(uri);
    return pjsip_param_find(&sip_uri->other_param, &name) != NULL;
}

static pj_status_t rewrite_register_to(pjsip_tx_data *tdata)
{
    pjsip_msg *msg = tdata->msg;
    pjsip_from_hdr *from;
    pjsip_to_hdr *to;
    pjsip_sip_uri *to_uri;
    pjsip_param *otg;
    pj_str_t name = pj_str("otg");

    if (msg->type != PJSIP_REQUEST_MSG ||
        pj_stricmp2(&msg->line.req.method.name, "REGISTER") != 0)
        return PJ_SUCCESS;

    from = (pjsip_from_hdr *)pjsip_msg_find_hdr(msg, PJSIP_H_FROM, NULL);
    to = (pjsip_to_hdr *)pjsip_msg_find_hdr(msg, PJSIP_H_TO, NULL);
    if (!from || !to || !uri_has_otg(from->uri))
        return PJ_SUCCESS;

    if (!PJSIP_URI_SCHEME_IS_SIP(to->uri) && !PJSIP_URI_SCHEME_IS_SIPS(to->uri))
        return PJ_SUCCESS;

    to_uri = (pjsip_sip_uri *)pjsip_uri_get_uri(to->uri);
    otg = pjsip_param_find(&to_uri->other_param, &name);
    if (otg)
        pj_list_erase(otg);

    return PJ_SUCCESS;
}

static pjsip_module lgw_rewrite_module = {0};

pj_status_t wxcalls_register_lgw_rewrite_module(void)
{
    pjsip_endpoint *endpoint = pjsua_get_pjsip_endpt();
    if (!endpoint)
        return PJ_EINVALIDOP;

    lgw_rewrite_module.name.ptr = "mod-wxcalls-lgw-to";
    lgw_rewrite_module.name.slen = 18;
    lgw_rewrite_module.id = -1;
    lgw_rewrite_module.priority = PJSIP_MOD_PRIORITY_APPLICATION;
    lgw_rewrite_module.on_tx_request = &rewrite_register_to;
    return pjsip_endpt_register_module(endpoint, &lgw_rewrite_module);
}

void wxcalls_reset_lgw_rewrite_module(void)
{
    /* PJSUA destroys the endpoint and its module list during libDestroy(). */
    lgw_rewrite_module.id = -1;
}
