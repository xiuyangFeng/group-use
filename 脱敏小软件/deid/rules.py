"""脱敏规则表。

参考 DICOM PS3.15 附录 E（Basic Application Level Confidentiality Profile），
并采用 "Retain Patient Characteristics"（保留性别、年龄、身高、体重）选项。
设备型号、层厚、重建核等技术参数一律保留，供分割/标注使用。

除了这里逐个列出的字段，anonymizer 还会按值类型（VR）做兜底：
  - PN（人名）：全部清空（PatientName 换成编号）
  - DA / DT（日期、日期时间）：全部清空
  - UI（UID）：除标准类 UID 外全部重新生成
  - AE（网络节点名）：全部清空
  - 私有字段、Overlay（60xx）、Curve（50xx）：全部删除
"""

# 直接删除的字段
REMOVE = {
    # ---- 患者身份 ----
    "PatientBirthTime", "PatientBirthName", "PatientMotherBirthName",
    "OtherPatientIDs", "OtherPatientNames", "OtherPatientIDsSequence",
    "IssuerOfPatientID", "IssuerOfPatientIDQualifiersSequence",
    "PatientAddress", "PatientTelephoneNumbers", "PatientTelecomInformation",
    "MilitaryRank", "BranchOfService", "MedicalRecordLocator",
    "PatientInsurancePlanCodeSequence", "PatientReligiousPreference",
    "PatientComments", "AdditionalPatientHistory", "Occupation",
    "CountryOfResidence", "RegionOfResidence", "EthnicGroup",
    "PatientBirthDateInAlternativeCalendar", "PatientDeathDateInAlternativeCalendar",
    "PatientAlternativeCalendar", "ResponsiblePerson", "ResponsiblePersonRole",
    "ResponsibleOrganization", "MedicalAlerts", "Allergies", "SmokingStatus",
    "PregnancyStatus", "LastMenstrualDate", "SpecialNeeds", "PatientState",
    "PatientPrimaryLanguageCodeSequence", "ReferencedPatientSequence",
    "ReferencedPatientPhotoSequence", "PatientInstitutionResidence",
    "CurrentPatientLocation",
    # ---- 就诊 / 申请 ----
    "AdmissionID", "IssuerOfAdmissionIDSequence", "AdmittingDiagnosesDescription",
    "AdmittingDiagnosesCodeSequence", "RouteOfAdmissions",
    "DischargeDiagnosisDescription", "VisitComments", "ServiceEpisodeID",
    "ServiceEpisodeDescription", "StudyComments", "ReasonForStudy",
    "RequestAttributesSequence", "RequestedProcedureID",
    "RequestedProcedureDescription", "ReasonForTheRequestedProcedure",
    "RequestingPhysician", "RequestingService", "ScheduledProcedureStepID",
    "ScheduledProcedureStepDescription", "PerformedProcedureStepID",
    "PerformedProcedureStepDescription", "PerformedLocation",
    "PlacerOrderNumberImagingServiceRequest",
    "FillerOrderNumberImagingServiceRequest", "OrderEnteredBy",
    "OrderEntererLocation", "OrderCallbackPhoneNumber",
    "ReferencedStudySequence", "ReferencedPerformedProcedureStepSequence",
    "ConfidentialityConstraintOnPatientDataDescription",
    # ---- 医院 / 医生 / 操作者 ----
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
    "InstitutionCodeSequence", "InstitutionalDepartmentTypeCodeSequence",
    "ReferringPhysicianAddress", "ReferringPhysicianTelephoneNumbers",
    "ReferringPhysicianIdentificationSequence", "ConsultingPhysicianName",
    "PhysiciansOfRecord", "PhysiciansOfRecordIdentificationSequence",
    "PerformingPhysicianName", "PerformingPhysicianIdentificationSequence",
    "NameOfPhysiciansReadingStudy", "PhysiciansReadingStudyIdentificationSequence",
    "OperatorsName", "OperatorIdentificationSequence",
    "ScheduledPerformingPhysicianName", "PersonName", "PersonAddress",
    "PersonTelephoneNumbers", "VerifyingObserverName", "VerifyingOrganization",
    "ContentCreatorName", "ReviewerName", "AuthorObserverSequence",
    "ParticipantSequence", "CustodialOrganizationSequence",
    # ---- 设备 / 网络节点 ----
    "StationName", "DeviceSerialNumber", "DetectorID", "PerformedStationName",
    "PerformedStationAETitle", "ScheduledStationName", "ScheduledStationAETitle",
    "ContributingEquipmentSequence",
    # ---- 自由文本 / 注释 ----
    "ImageComments", "FrameComments", "AcquisitionComments", "TextComments",
    "TextString", "InterpretationText", "DerivationDescription",
    # ---- 其他可能藏信息的结构 ----
    "IconImageSequence",            # 缩略图，可能带烧录文字
    "OriginalAttributesSequence",   # 修改前的原值备份
    "ModifiedAttributesSequence",
    "DigitalSignaturesSequence", "MACParametersSequence",
    "EncryptedAttributesSequence",
    "StorageMediaFileSetID", "StorageMediaFileSetUID",
    "DataSetTrailingPadding",       # 文件尾部填充，可能残留任意旧数据
    "TimezoneOffsetFromUTC",
}

# 保留字段但清空值（DICOM 规定这些字段必须存在）
EMPTY = {
    "AccessionNumber", "StudyID", "ReferringPhysicianName", "PatientBirthDate",
}

# file meta 里需要删除的字段
FILE_META_REMOVE = {
    "SourceApplicationEntityTitle", "SendingApplicationEntityTitle",
    "ReceivingApplicationEntityTitle", "PrivateInformationCreatorUID",
    "PrivateInformation",
}

# 这些 UID 是"类型"标识而不是实例标识，不能改
KEEP_UID_KEYWORDS = {
    "TransferSyntaxUID", "ImplementationClassUID", "CodingSchemeUID",
    "ContextUID", "MappingResourceUID", "ContextGroupExtensionCreatorUID",
}

# 疑似图像中烧录了文字的序列（只提示，不处理）
BURNED_IN_SOP_CLASS_PREFIX = "1.2.840.10008.5.1.4.1.1.7"  # Secondary Capture 家族
BURNED_IN_MODALITIES = {"SC", "OT"}
BURNED_IN_KEYWORDS = ("DOSE", "SCREEN", "REPORT", "SUMMARY", "SNAPSHOT", "CAPTURE", "VOLUME RENDERING",
                      "剂量", "报告", "截图")  # 序列描述里的关键词（弱信号）
# ImageType 里的这些词是设备自动写的，说明是屏幕截图/三维渲染图（强信号）
BURNED_IN_IMAGE_TYPES = ("SCREEN SAVE", "SCREENSAVE", "SCREEN_SAVE", "VOLREND", "VOLUME RENDER", "CAPTURE", "SNAPSHOT")
# 彩色图像：CT 原始断层永远是灰度的，彩色的是三维渲染截图、报告等（强信号）
COLOR_PHOTOMETRIC = ("RGB", "YBR", "PALETTE")
