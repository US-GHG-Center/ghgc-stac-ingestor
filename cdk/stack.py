import os
from typing import Dict, Union

from aws_cdk import (
    Aspects,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigateway,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda,
    aws_lambda_event_sources as events,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

from .config import Deployment


class StacIngestionApi(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: Deployment,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if config.permissions_boundary_policy_name:
            permission_boundary_policy = iam.ManagedPolicy.from_managed_policy_name(
                self,
                "permission-boundary",
                config.permissions_boundary_policy_name,
            )
            iam.PermissionsBoundary.of(self).apply(permission_boundary_policy)

            from cdk.permission_boundary import PermissionBoundaryAspect

            Aspects.of(self).add(PermissionBoundaryAspect(permission_boundary_policy))

        table = self.build_table()
        jwks_url = self.build_jwks_url(config.userpool_id)

        data_access_role = (
            iam.Role.from_role_arn(self, "data-access-role", config.data_access_role)
            if config.data_access_role
            else None
        )

        user_pool = cognito.UserPool.from_user_pool_id(
            self, "cognito-user-pool", config.userpool_id
        )

        # subset of env needed for lambdas
        # TODO we may be able to further refine these for our 2 lambdas
        lambda_env_keys = [
            "DYNAMODB_TABLE",
            "JWKS_URL",
            # "ROOT_PATH",
            "NO_PYDANTIC_SSM_SETTINGS",
            "STAC_URL",
            "DATA_ACCESS_ROLE",
            "USERPOOL_ID",
            "CLIENT_ID",
            "MWAA_ENV",
            "RASTER_URL",
            "PATH_PREFIX",
        ]

        env = {
            "DYNAMODB_TABLE": table.table_name,
            "JWKS_URL": jwks_url,
            # "ROOT_PATH": f"/{config.stage}",
            "NO_PYDANTIC_SSM_SETTINGS": "1",
            "STAC_URL": config.stac_url,
            "DATA_ACCESS_ROLE": config.data_access_role or "",
            "USERPOOL_ID": config.userpool_id,
            "CLIENT_ID": config.client_id,
            "MWAA_ENV": config.mwaa_env,
            "RASTER_URL": config.raster_url,
            "STAC_DB_SECRET_NAME": config.stac_db_secret_name,
            "STAC_DB_VPC_ID": config.stac_db_vpc_id,
            "STAC_DB_SECURITY_GROUP_ID": config.stac_db_security_group_id,
            "STAC_DB_PUBLIC_SUBNET": config.stac_db_public_subnet,
            "PATH_PREFIX": config.path_prefix,
        }

        db_secret = self.get_db_secret(config.stac_db_secret_name, config.stage)
        db_vpc = ec2.Vpc.from_lookup(self, "vpc", vpc_id=config.stac_db_vpc_id)
        db_security_group = ec2.SecurityGroup.from_security_group_id(
            self,
            "db-security-group",
            security_group_id=config.stac_db_security_group_id,
        )

        lambda_env = {k: env[k] for k in lambda_env_keys if env.get(k)}

        handler = self.build_api_lambda(
            table=table,
            env=lambda_env,
            data_access_role=data_access_role,
            user_pool=user_pool,
            db_secret=db_secret,
            db_vpc=db_vpc,
            db_security_group=db_security_group,
            db_subnet_public=config.stac_db_public_subnet,
        )

        self.ingestor_api = self.build_api(
            handler=handler,
            stage=config.stage,
        )

        self.build_ingestor(
            table=table,
            env=lambda_env,
            db_secret=db_secret,
            db_vpc=db_vpc,
            db_security_group=db_security_group,
            db_subnet_public=config.stac_db_public_subnet,
        )

        self.register_ssm_parameter(
            name="ingestor_url",
            value=self.ingestor_api.url,
            description="URL for ingestor",
        )
        self.register_ssm_parameter(
            name="jwks_url",
            value=jwks_url,
            description="JWKS URL for Cognito user pool",
        )
        self.register_ssm_parameter(
            name="dynamodb_table",
            value=table.table_name,
            description="Name of table used to store ingestions",
        )

    def build_jwks_url(self, userpool_id: str) -> str:
        region = userpool_id.split("_")[0]
        return (
            f"https://cognito-idp.{region}.amazonaws.com"
            f"/{userpool_id}/.well-known/jwks.json"
        )

    def build_table(self) -> dynamodb.ITable:
        table = dynamodb.Table(
            self,
            "ingestions-table",
            partition_key={"name": "created_by", "type": dynamodb.AttributeType.STRING},
            sort_key={"name": "id", "type": dynamodb.AttributeType.STRING},
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
        )
        table.add_global_secondary_index(
            index_name="status",
            partition_key={"name": "status", "type": dynamodb.AttributeType.STRING},
            sort_key={"name": "created_at", "type": dynamodb.AttributeType.STRING},
        )
        return table

    def build_api_lambda(
        self,
        *,
        table: dynamodb.ITable,
        env: Dict[str, str],
        data_access_role: Union[iam.IRole, None],
        user_pool: cognito.IUserPool,
        db_secret: secretsmanager.ISecret,
        db_vpc: ec2.IVpc,
        db_security_group: ec2.ISecurityGroup,
        db_subnet_public: bool,
        code_dir: str = "./",
    ) -> apigateway.LambdaRestApi:
        handler_role = iam.Role(
            self,
            "execution-role",
            description=(
                "Role used by STAC Ingestor. Manually defined so that we can choose a "
                "name that is supported by the data access roles trust policy"
            ),
            role_name=f"{Stack.of(self).stack_name}-lambda-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
        )
        handler = aws_lambda.Function(
            self,
            "api-handler",
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(code_dir),
                file="api/Dockerfile",
                platform="linux/amd64",
            ),
            runtime=aws_lambda.Runtime.PYTHON_3_9,
            timeout=Duration.seconds(30),
            handler="handler.handler",
            role=handler_role,
            environment={"DB_SECRET_ARN": db_secret.secret_arn, **env},
            vpc=db_vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
                if db_subnet_public
                else ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            allow_public_subnet=True,
            memory_size=2048,
        )
        table.grant_read_write_data(handler)
        if data_access_role:
            data_access_role.grant(
                handler.grant_principal,
                "sts:AssumeRole",
            )

        # Give read access to any bucket/key
        handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:GetObject"],
                resources=["arn:aws:s3:::*"],
            )
        )

        handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminInitiateAuth"],
                resources=[user_pool.user_pool_arn],
            )
        )

        if mwaa_env := env.get("MWAA_ENV"):
            handler.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["airflow:CreateCliToken"],
                    resources=[
                        f"arn:aws:airflow:{self.region}:{self.account}:environment/{mwaa_env}"
                    ],
                )
            )

        # Allow handler to read DB secret
        db_secret.grant_read(handler)

        # Allow handler to connect to DB
        db_security_group.add_ingress_rule(
            peer=handler.connections.security_groups[0],
            connection=ec2.Port.tcp(5432),
            description="Allow connections from STAC Ingestor",
        )
        return handler

    def build_ingestor(
        self,
        *,
        table: dynamodb.ITable,
        env: Dict[str, str],
        db_secret: secretsmanager.ISecret,
        db_vpc: ec2.IVpc,
        db_security_group: ec2.ISecurityGroup,
        db_subnet_public: bool,
        code_dir: str = "./",
    ) -> aws_lambda.Function:
        handler = aws_lambda.Function(
            self,
            "stac-ingestor",
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(code_dir),
                file="api/Dockerfile",
                platform="linux/amd64",
            ),
            handler="ingestor.handler",
            runtime=aws_lambda.Runtime.PYTHON_3_9,
            timeout=Duration.seconds(180),
            environment={"DB_SECRET_ARN": db_secret.secret_arn, **env},
            vpc=db_vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
                if db_subnet_public
                else ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            allow_public_subnet=True,
            memory_size=2048,
        )

        # Allow handler to read DB secret
        db_secret.grant_read(handler)

        # Allow handler to connect to DB
        db_security_group.add_ingress_rule(
            peer=handler.connections.security_groups[0],
            connection=ec2.Port.tcp(5432),
            description="Allow connections from STAC Ingestor",
        )

        # Allow handler to write results back to DBƒ
        table.grant_write_data(handler)

        # Trigger handler from writes to DynamoDB table
        handler.add_event_source(
            events.DynamoEventSource(
                table=table,
                # Read when batches reach size...
                batch_size=1000,
                # ... or when window is reached.
                max_batching_window=Duration.seconds(10),
                # Read oldest data first.
                starting_position=aws_lambda.StartingPosition.TRIM_HORIZON,
                retry_attempts=1,
            )
        )

        return handler

    def build_api(
        self,
        *,
        handler: aws_lambda.IFunction,
        stage: str,
    ) -> apigateway.LambdaRestApi:
        return apigateway.LambdaRestApi(
            self,
            f"{Stack.of(self).stack_name}-api",
            handler=handler,
            cloud_watch_role=True,
            deploy_options=apigateway.StageOptions(stage_name=stage),
        )

    def get_db_secret(self, secret_name: str, stage: str) -> secretsmanager.ISecret:
        return secretsmanager.Secret.from_secret_name_v2(
            self, f"pgstac-db-secret-{stage}", secret_name
        )

    def register_ssm_parameter(
        self,
        name: str,
        value: str,
        description: str,
    ) -> ssm.IStringParameter:
        parameter_namespace = Stack.of(self).stack_name
        return ssm.StringParameter(
            self,
            f"{name.replace('_', '-')}-parameter",
            description=description,
            parameter_name=f"/{parameter_namespace}/{name}",
            string_value=value,
        )
