from typing import Optional

from pydantic import AnyHttpUrl, BaseSettings, Field, constr
from pydantic_ssm_settings import AwsSsmSourceConfig

AwsArn = constr(regex=r"^arn:aws:iam::\d{12}:role/.+")
AwsStepArn = constr(regex=r"^arn:aws:states:.+:\d{12}:stateMachine:.+")


class Settings(BaseSettings):
    dynamodb_table: str

    root_path: Optional[str] = Field(description="Path from where to serve this URL.")

    jwks_url: AnyHttpUrl = Field(
        description="URL of JWKS, e.g. https://cognito-idp.{region}.amazonaws.com/{userpool_id}/.well-known/jwks.json"  # noqa
    )

    stac_url: AnyHttpUrl = Field(description="URL of STAC API")

    # See validate_dataset() in main.py
    raster_url: AnyHttpUrl = Field(description="URL of Raster API")

    data_access_role: Optional[AwsArn] = Field(
        description="ARN of AWS Role used to validate access to S3 data"
    )

    userpool_id: str = Field(description="The Cognito Userpool used for authentication")

    client_id: str = Field(description="The Cognito APP client ID")

    path_prefix: Optional[str] = Field(
        "",
        description="Optional path prefix to add to all api endpoints",
    )

    class Config(AwsSsmSourceConfig):
        env_file = ".env"

    @classmethod
    def from_ssm(cls, stack: str):
        return cls(_secrets_dir=f"/{stack}")
