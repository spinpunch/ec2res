# ec2res
Script that checks coverage of EC2 and RDS reserved instances.

Run with AWS credentials in your environment.

Example:

```
$ AWS_PROFILE=myprofile ./ec2res.py 
EC2 INSTANCES:
instance1                 us-east-1a --VPC-- t2.small    (region)   --VPC-- t2.small                  $118/yr   98 days left aabbccdd...
EC2 UNUSED RESERVATIONS: (none)
RDS INSTANCES:
rds01                 us-east-1c NoMulti db.t2.micro   postgres NOT COVERED
RDS UNUSED RESERVATIONS: (none)
