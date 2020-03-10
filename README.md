# charm-apache-httpd

An example charm written in the Operator Framework which deploys apache2 and configures a vhost.

# Usage

```
juju deploy cs:~dmitriis/apache-httpd
juju deploy cs:~dmitriis/dummy-vhost
juju relate apache-httpd dummy-vhost
juju add-unit apache-httpd
```

The content provided by a vhost will be available at the http://<machine-address>:80.
