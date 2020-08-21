# coding: utf-8

from datetime import date, datetime

from typing import List, Dict, Type

from .base_model_ import Model
from .. import util


class InlineResponse2002DataServiceBuildDetails(Model):
    """NOTE: This class is auto generated by OpenAPI Generator (https://openapi-generator.tech).

    Do not edit the class manually.
    """

    def __init__(self, build_date: str=None, vcs_ref: str=None, vcs_url: str=None):
        """InlineResponse2002DataServiceBuildDetails - a model defined in OpenAPI

        :param build_date: The build_date of this InlineResponse2002DataServiceBuildDetails.
        :param vcs_ref: The vcs_ref of this InlineResponse2002DataServiceBuildDetails.
        :param vcs_url: The vcs_url of this InlineResponse2002DataServiceBuildDetails.
        """
        self.openapi_types = {
            'build_date': str,
            'vcs_ref': str,
            'vcs_url': str
        }

        self.attribute_map = {
            'build_date': 'build_date',
            'vcs_ref': 'vcs_ref',
            'vcs_url': 'vcs_url'
        }

        self._build_date = build_date
        self._vcs_ref = vcs_ref
        self._vcs_url = vcs_url

    @classmethod
    def from_dict(cls, dikt: dict) -> 'InlineResponse2002DataServiceBuildDetails':
        """Returns the dict as a model

        :param dikt: A dict.
        :return: The inline_response_200_2_data_service_build_details of this InlineResponse2002DataServiceBuildDetails.
        """
        return util.deserialize_model(dikt, cls)

    @property
    def build_date(self):
        """Gets the build_date of this InlineResponse2002DataServiceBuildDetails.


        :return: The build_date of this InlineResponse2002DataServiceBuildDetails.
        :rtype: str
        """
        return self._build_date

    @build_date.setter
    def build_date(self, build_date):
        """Sets the build_date of this InlineResponse2002DataServiceBuildDetails.


        :param build_date: The build_date of this InlineResponse2002DataServiceBuildDetails.
        :type build_date: str
        """

        self._build_date = build_date

    @property
    def vcs_ref(self):
        """Gets the vcs_ref of this InlineResponse2002DataServiceBuildDetails.


        :return: The vcs_ref of this InlineResponse2002DataServiceBuildDetails.
        :rtype: str
        """
        return self._vcs_ref

    @vcs_ref.setter
    def vcs_ref(self, vcs_ref):
        """Sets the vcs_ref of this InlineResponse2002DataServiceBuildDetails.


        :param vcs_ref: The vcs_ref of this InlineResponse2002DataServiceBuildDetails.
        :type vcs_ref: str
        """

        self._vcs_ref = vcs_ref

    @property
    def vcs_url(self):
        """Gets the vcs_url of this InlineResponse2002DataServiceBuildDetails.


        :return: The vcs_url of this InlineResponse2002DataServiceBuildDetails.
        :rtype: str
        """
        return self._vcs_url

    @vcs_url.setter
    def vcs_url(self, vcs_url):
        """Sets the vcs_url of this InlineResponse2002DataServiceBuildDetails.


        :param vcs_url: The vcs_url of this InlineResponse2002DataServiceBuildDetails.
        :type vcs_url: str
        """

        self._vcs_url = vcs_url
