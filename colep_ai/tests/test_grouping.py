from ingestion_V2.image_group_resolver import associate_ocr_with_images
from ingestion_V2.image_combiner import group_image_ids_by_step
from core.config import settings
import anthropic
ip=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputs\e5\crops\page_3\marked\page_3_marked.png"

ocr= "colep Packaging\nParametrizar Máquina de Soldar L5-001.0067.1\nLigar e desligar a máquina\nPara ligar o geral da linha realizar a Fase 1\nPara ligar a máquina e colocar em funcionamento realizar as Fases de 2 até 3\nPara soldar por unidade realizar a Fase 4\nPara parar e desligar a máquina realizar as Fases de 5 até 6\nPara desligar o geral da linha realizar a Fase 7\nNOTA: O geral da linha desliga tudo.\n1 Ligar o geral da linha no quadro elétrico da linha.\nNota: caso o geral da máquina de soldar se encontre desligado deve\nMAQUINA DE SOLDAR\n3\nPara colocar a máquina em funcionamento contínuo:\n- garantir que o botão (I) 'Man-Aut' está para fora.\n- garantir que o botão (E) 'Desempilhador' está para fora.\n- ligar os botões K, L, M e N.\n10\nKLMN\nGERAL\n4\nPara colocar a máquina a soldar por unidade:\nGERAL\nEmergência\nLigar o geral da máquina de soldar no botão indicado na figura.\n- garantir que o botão (I) 'Man-Aut' está para dentro.\ngarantir que o botão (E) 'Desempilhador' está para fora.\n- ligar os botões K, L, M e manter pressionado o botão N enquanto\ndesejar soldar por unidade.\nAO\nElaborado por: Matilde Martins\nData: 05/11/2025\nAprovado por: Filipe Oliveira\nData:10-11-2025\nQ00.M042.1\nÁrea/Linha:\nL5 Montagem\nCópias em papel não controladas, salvo indicação em contrário.\nEste documento é estritamente confidencial e permanece propriedade da Colep Packaging\n5\nDesligar o geral da máquina de soldar no botão indicado na\nfigura.\nAO\n6\nDesligar o geral da linha no quadro elétrico.\nCONTADOR\nGERAL\nGERAL\nMERLIN CARN\nÂmbito\nPágina No\nOperacional (O)\n3/23"

crops_metadata=[{'image_id': 'page_3_9a3c', 'bbox': [1026, 94, 1251, 157], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_9a3c.png'}, {'image_id': 'page_3_7b9c', 'bbox': [1584, 355, 1825, 658], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_7b9c.png'}, {'image_id': 'page_3_af69', 'bbox': [886, 419, 1348, 746], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_af69.png'}, {'image_id': 'page_3_ff3a', 'bbox': [463, 620, 716, 757], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_ff3a.png'}, {'image_id': 'page_3_0880', 'bbox': [247, 638, 420, 868], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_0880.png'}, {'image_id': 'page_3_51c3', 'bbox': [1438, 766, 1942, 983], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_51c3.png'}, {'image_id': 'page_3_5e18', 'bbox': [461, 767, 716, 902], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_5e18.png'}, {'image_id': 'page_3_fd37', 'bbox': [377, 957, 618, 1261], 'crop_path': 'D:\\Harpreet Data\\1_PROJECTS\\Colep_ai\\colepV1\\outputs\\e5\\crops\\page_3\\crops\\page_3_fd37.png'}]


claude_client = anthropic.Anthropic()
excel_path=r"docs\e5.xlsx"
p=3
res=associate_ocr_with_images(ip,ocr,crops_metadata,excel_path,p,claude_client)
import json
print(json.dumps(res, ensure_ascii=False, indent=2))

ids=group_image_ids_by_step(res)
print("*"*30)
print(ids)