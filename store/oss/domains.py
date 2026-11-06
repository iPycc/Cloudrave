"""Bucket domain operations. DNS and certificates remain console-managed."""

from urllib.parse import urlsplit

import alibabacloud_oss_v2 as oss

from store.oss.download import domain_url


def manage_domains(client, bucket: str, region: str, action: str, domain: str = "") -> dict:
    host = urlsplit(domain_url(domain)).hostname if domain else ""
    if action != "list" and not host:
        raise ValueError("请填写自定义域名")
    result = {"cname_target": f"{bucket}.oss-{region}.aliyuncs.com"}
    configuration = oss.BucketCnameConfiguration(cname=oss.Cname(domain=host))
    if action == "token":
        response = client.create_cname_token(oss.CreateCnameTokenRequest(
            bucket=bucket, bucket_cname_configuration=configuration,
        ))
        token = response.cname_token
        if token is None or not token.token:
            raise ValueError("OSS 未返回验证记录；请查询域名是否已经绑定，或直接尝试绑定。")
        result.update(domain=host, txt_name=f"_dnsauth.{host}", token=token.token,
                      expires_at=str(token.expire_time or ""))
    elif action == "bind":
        client.put_cname(oss.PutCnameRequest(bucket=bucket, bucket_cname_configuration=configuration))
        result.update(domain=host, status="bound")
    elif action == "unbind":
        client.delete_cname(oss.DeleteCnameRequest(bucket=bucket, bucket_cname_configuration=configuration))
        result.update(domain=host, status="unbound")
    elif action != "list":
        raise ValueError("不支持的域名操作")
    if action == "list":
        response = client.list_cname(oss.ListCnameRequest(bucket=bucket))
        result["domains"] = [
            {"domain": item.domain, "status": item.status}
            for item in response.cnames or []
        ]
    return result
